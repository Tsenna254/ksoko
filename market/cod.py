"""Pay on Delivery: the seller's listing fee, and the seller's side of cash-on-delivery orders.

How it works
  * A seller who turns on "Offer Pay on Delivery" pays a one-time fee (COD_LISTING_FEE_PERCENT, 10% by default) of the
    item's value (price x quantity), when they list it. The fee is paid online and confirmed by staff.
  * Until it is confirmed the item still sells, prepaid only. Raising the price or quantity later creates a
    top-up fee for the extra value, and Pay on Delivery pauses until that is paid too.
  * A buyer's cash-on-delivery order waits as "Pay on delivery". The seller delivers, collects the cash and marks it
    delivered; staff confirm, and only then does it count as a sale. Staff can also mark it "not delivered", which
    returns the stock. Buyers with repeated failed deliveries lose Pay on Delivery.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import audit
from .forms import FeePaymentForm
from .gateways import GatewayError
from .models import Listing, ListingFee, Order, Payment
from .payments import start_gateway
from .throttle import limited, too_many

FEE = ListingFee.Status


def fee_for(value):
    """The fee on a given value: a percentage, rounded to whole shillings, at least KES 1."""
    return max(Decimal("1"), (value * settings.COD_LISTING_FEE_PERCENT / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def sync_cod_fee(listing):
    """Make the unpaid fee on this listing match the value that still needs covering.

    Returns the fee the seller still has to pay, or None when nothing is due.
    """
    fees = list(ListingFee.objects.filter(listing=listing).select_related("payment"))
    paid = sum((f.covers_value for f in fees if f.status == FEE.PAID), Decimal("0"))
    unpaid = [f for f in fees if f.status == FEE.UNPAID]
    in_progress = [f for f in unpaid if f.payment and f.payment.is_open]
    loose = [f for f in unpaid if f not in in_progress]

    wanted = listing.allow_cod and listing.listing_type == Listing.Type.BUY
    need = listing.price * listing.quantity - paid if wanted else Decimal("0")
    if need <= 0:
        for fee in loose:
            fee.delete()
        return None
    if in_progress:                                   # being paid right now: leave it alone
        return in_progress[0]
    fee = loose[0] if loose else ListingFee(listing=listing, seller=listing.seller)
    for extra in loose[1:]:
        extra.delete()
    fee.covers_value, fee.percent, fee.amount = need, settings.COD_LISTING_FEE_PERCENT, fee_for(need)
    fee.save()
    return fee


# ---- Seller pays the listing fee -------------------------------------------------------------

@login_required
def listing_fee_pay(request, pk):
    listing = get_object_or_404(Listing, pk=pk, seller=request.user)
    fee = sync_cod_fee(listing)
    if fee is None:
        messages.info(request, "There's no listing fee due on this item.")
        return redirect(listing)
    if fee.payment_id and fee.payment.is_open:
        return redirect("payment_detail", reference=fee.payment.reference)
    form = FeePaymentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if limited(request, "feepay", 10, 3600, ident=request.user.pk):
            return too_many(request)
        with transaction.atomic():
            payment = Payment.objects.create(
                user=request.user, purpose=Payment.Purpose.LISTING_FEE, method=form.cleaned_data["method"],
                phone=form.cleaned_data["phone"], amount=fee.amount,
            )
            fee.payment = payment
            fee.save(update_fields=["payment"])
        try:
            url = start_gateway(request, payment)
        except GatewayError as exc:
            messages.error(request, str(exc))
            url = None
        return redirect(url or reverse("payment_detail", args=[payment.reference]))
    return render(request, "market/listing_fee.html", {"listing": listing, "fee": fee, "form": form})


# ---- Seller's sales and cash-on-delivery steps -----------------------------------------------

@login_required
def seller_sales(request):
    """Orders for this seller's items, with delivery details once the buyer has paid or chosen Pay on Delivery."""
    payments = (Payment.objects.filter(orders__seller=request.user, purpose=Payment.Purpose.ORDER)
                .exclude(status__in=[Payment.Status.FAILED, Payment.Status.CANCELLED, Payment.Status.REJECTED])
                .distinct().select_related("user").prefetch_related("orders__listing").order_by("-created_at"))
    rows = []
    for p in payments:
        mine = [o for o in p.orders.all() if o.seller_id == request.user.id]    # never show other sellers' items
        rows.append({
            "payment": p, "orders": mine, "total": sum((o.total for o in mine), Decimal("0")),
            "show_details": p.status in (Payment.Status.COD_PENDING, Payment.Status.PAID, Payment.Status.CONFIRMED),
        })
    return render(request, "market/seller_sales.html", {"rows": rows})


def _cod_payment_for_seller(request, reference):
    payment = get_object_or_404(Payment, reference=reference, method=Payment.Method.COD, status=Payment.Status.COD_PENDING)
    if not payment.orders.filter(seller=request.user).exists() or payment.orders.exclude(seller=request.user).exists():
        raise Http404
    return payment


@login_required
@require_POST
def sale_delivered(request, reference):
    payment = _cod_payment_for_seller(request, reference)
    if payment.mark_cod_delivered():
        audit.log(request, "cod.delivered", payment.reference, f"KES {payment.amount}")
        messages.success(request, "Marked as delivered. Our team will confirm it shortly.")
    return redirect("seller_sales")


@login_required
@require_POST
def sale_failed(request, reference):
    payment = _cod_payment_for_seller(request, reference)
    reason = request.POST.get("reason", "").strip()[:150] or "no reason given"
    payment.note = f"Seller reports delivery failed: {reason}"[:255]
    payment.save(update_fields=["note"])
    audit.log(request, "cod.delivery_failed_reported", payment.reference, reason)
    messages.info(request, "Thanks. Our team will review it and update the order.")
    return redirect("seller_sales")
