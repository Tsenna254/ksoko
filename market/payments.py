import hmac
import json
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import gateways
from .cart import cart_summary
from .forms import CheckoutForm
from .gateways import GatewayError
from .models import CartItem, Listing, Order, Payment
from .security import client_ip
from .throttle import limited


class CheckoutError(Exception):
    pass


@transaction.atomic
def create_payment(user, method, phone):
    """Turn the cart (and any won auctions) into pending orders under one Payment.

    Stock is reserved straight away. It is returned if the payment fails or is rejected.
    """
    items = list(CartItem.objects.filter(user=user))
    locked = {l.pk: l for l in Listing.objects.select_for_update().filter(pk__in=[i.listing_id for i in items])}
    unpaid = list(Order.objects.select_for_update().filter(buyer=user, status=Order.Status.UNPAID, payment__isnull=True))
    if not items and not unpaid:
        raise CheckoutError("Your cart is empty.")

    orders = []
    for item in items:
        listing = locked[item.listing_id]
        if (listing.listing_type != Listing.Type.BUY or not listing.is_open
                or listing.quantity < item.quantity or listing.seller_id == user.id):
            raise CheckoutError(f"“{listing.title}” is no longer available in that quantity. Update your cart and try again.")
        orders.append(Order(
            listing=listing, buyer=user, seller=listing.seller, title=listing.title, source=Order.Source.BUY,
            quantity=item.quantity, amount=listing.price * item.quantity, delivery_fee=listing.delivery_fee,
            status=Order.Status.PENDING,
        ))
        listing.quantity -= item.quantity
        if listing.quantity == 0:
            listing.status = Listing.Status.SOLD
        listing.save(update_fields=["quantity", "status", "updated_at"])

    total = sum((o.amount + o.delivery_fee for o in orders), Decimal("0")) + sum((o.total for o in unpaid), Decimal("0"))
    payment = Payment.objects.create(user=user, method=method, phone=phone, amount=total)
    for order in orders:
        order.payment = payment
        order.save()
    for order in unpaid:
        order.payment, order.status = payment, Order.Status.PENDING
        order.save(update_fields=["payment", "status"])
    CartItem.objects.filter(user=user).delete()
    return payment


def start_gateway(request, payment):
    """Kick off the payment with the right provider. Returns a URL to redirect to, or None."""
    if payment.method == Payment.Method.MPESA:
        if gateways.mpesa_stk_ready():
            payment.mpesa_checkout_id = gateways.mpesa_stk_push(payment.phone, payment.amount, payment.reference)
            payment.save(update_fields=["mpesa_checkout_id"])
            messages.success(request, f"We sent a payment prompt to {payment.phone}. Enter your M-Pesa PIN to pay.")
        return None  # otherwise the payment page shows the manual Paybill/Till steps
    if gateways.card_ready():
        return_url = settings.SITE_URL.rstrip("/") + reverse("card_return", args=[payment.reference])
        return gateways.card_checkout_link(payment, request.user, return_url)
    if not settings.PAYMENTS_TEST_MODE:
        raise GatewayError("Card payments aren't available yet. Please choose M-Pesa.")
    return None


def _own_payment(request, reference):
    return get_object_or_404(Payment, reference=reference, user=request.user)


@login_required
def checkout(request):
    cart = cart_summary(request.user)
    if not cart["lines"] and not cart["unpaid"]:
        messages.info(request, "Your cart is empty.")
        return redirect("cart")
    if cart["blocked"]:
        messages.error(request, "Some items in your cart are no longer available. Update your cart first.")
        return redirect("cart")
    form = CheckoutForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            payment = create_payment(request.user, form.cleaned_data["method"], form.cleaned_data["phone"])
        except CheckoutError as exc:
            messages.error(request, str(exc))
            return redirect("cart")
        try:
            url = start_gateway(request, payment)
        except GatewayError as exc:  # the payment exists; the customer can retry from its page
            messages.error(request, str(exc))
            url = None
        return redirect(url or reverse("payment_detail", args=[payment.reference]))
    return render(request, "market/checkout.html", {"cart": cart, "form": form})


@login_required
def payment_detail(request, reference):
    payment = _own_payment(request, reference)
    return render(request, "market/payment_detail.html", {
        "payment": payment,
        "orders": payment.orders.all(),
        "stk_ready": gateways.mpesa_stk_ready(),
        "card_ready": gateways.card_ready(),
        "test_mode": settings.PAYMENTS_TEST_MODE,
        "manual_number": settings.MPESA_MANUAL_NUMBER,
        "manual_kind": settings.MPESA_MANUAL_KIND,
    })


@login_required
@require_POST
def payment_code(request, reference):
    """Manual M-Pesa: the customer paid the Paybill/Till and types in the confirmation code."""
    payment = _own_payment(request, reference)
    code = request.POST.get("code", "").strip().upper()
    if limited(request, "paycode", 10, 3600, ident=request.user.pk):
        messages.error(request, "Too many attempts. Please try again later or contact support.")
        return redirect("payment_detail", reference=reference)
    if payment.method != Payment.Method.MPESA or payment.status != Payment.Status.AWAITING:
        messages.error(request, "This payment can't accept a code right now.")
    elif not re.fullmatch(r"[A-Z0-9]{10}", code):
        messages.error(request, "An M-Pesa code has 10 letters and numbers, for example QGH7ABC123.")
    elif Payment.objects.filter(provider_ref=code).exclude(pk=payment.pk).exists():
        messages.error(request, "That M-Pesa code has already been used.")
    else:
        payment.mark_paid(code, "Customer submitted M-Pesa code")
        messages.success(request, "Thanks. We'll confirm your payment shortly.")
    return redirect("payment_detail", reference=reference)


@login_required
@require_POST
def payment_retry(request, reference):
    payment = _own_payment(request, reference)
    if payment.status != Payment.Status.AWAITING:
        return redirect("payment_detail", reference=reference)
    try:
        url = start_gateway(request, payment)
    except GatewayError as exc:
        messages.error(request, str(exc))
        url = None
    return redirect(url or reverse("payment_detail", args=[reference]))


@login_required
@require_POST
def payment_cancel(request, reference):
    payment = _own_payment(request, reference)
    if payment.status == Payment.Status.AWAITING and payment.release(Payment.Status.CANCELLED, "Cancelled by customer"):
        messages.info(request, "Payment cancelled. The items were returned to stock.")
    return redirect("payment_detail", reference=reference)


@login_required
@require_POST
def payment_test(request, reference):
    """Test mode only: pretend the customer paid, so the confirm step can be tried without a gateway."""
    if not settings.PAYMENTS_TEST_MODE:
        raise Http404
    payment = _own_payment(request, reference)
    if payment.mark_paid("TEST" + uuid.uuid4().hex[:8].upper(), "Test mode payment"):
        messages.success(request, "Test payment recorded. Staff can now confirm it in the dashboard.")
    return redirect("payment_detail", reference=reference)


@login_required
def card_return(request, reference):
    """The customer comes back from the hosted card page. Verify with the processor before trusting it."""
    payment = _own_payment(request, reference)
    if payment.status == Payment.Status.AWAITING:
        status, txn = request.GET.get("status", ""), request.GET.get("transaction_id", "")
        if status in ("successful", "completed") and txn.isdigit():
            try:
                data = gateways.card_verify(txn)
            except GatewayError as exc:
                messages.error(request, str(exc))
                return redirect("payment_detail", reference=reference)
            valid = (data.get("status") == "successful" and data.get("tx_ref") == payment.reference
                     and data.get("currency") == "KES" and Decimal(str(data.get("amount", 0))) >= payment.amount)
            if valid:
                payment.mark_paid(txn, "Card payment verified with the processor")
                messages.success(request, "Card payment received. We'll confirm it shortly.")
            else:
                payment.release(Payment.Status.FAILED, "Card payment could not be verified")
                messages.error(request, "We couldn't verify that card payment, so the order was cancelled.")
        else:
            messages.info(request, "The card payment wasn't completed. You can try again or cancel.")
    return redirect("payment_detail", reference=reference)


@csrf_exempt
@require_POST
def mpesa_callback(request, token):
    """Safaricom calls this after the customer answers the STK prompt.

    The URL carries a secret token (MPESA_CALLBACK_SECRET), and can be limited to Safaricom's IP addresses,
    so a stranger can't post a fake "paid" message. Staff still confirm every payment against the statement.
    """
    secret = settings.MPESA_CALLBACK_SECRET
    if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
        raise Http404
    if settings.MPESA_CALLBACK_IPS and client_ip(request) not in settings.MPESA_CALLBACK_IPS:
        raise Http404
    ok = JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})
    try:
        cb = json.loads(request.body)["Body"]["stkCallback"]
        payment = Payment.objects.get(mpesa_checkout_id=cb["CheckoutRequestID"])
    except (ValueError, KeyError, TypeError, Payment.DoesNotExist):
        return ok

    success = cb.get("ResultCode") == 0
    items = (cb.get("CallbackMetadata") or {}).get("Item", [])
    meta = {i.get("Name"): i.get("Value") for i in items if isinstance(i, dict)}
    receipt = str(meta.get("MpesaReceiptNumber", ""))[:60]
    try:
        paid = Decimal(str(meta.get("Amount", 0)))
    except InvalidOperation:
        paid = Decimal(0)

    def save_note(text, with_receipt=False):
        payment.note = text[:255]
        fields = ["note"]
        if with_receipt and receipt:
            payment.provider_ref = receipt
            fields.append("provider_ref")
        try:
            payment.save(update_fields=fields)
        except IntegrityError:                      # that M-Pesa receipt is already on another payment
            payment.note = f"CHECK BY HAND: duplicate M-Pesa receipt {receipt}"[:255]
            payment.save(update_fields=["note"])

    if payment.status != Payment.Status.AWAITING:
        if success and receipt and payment.status in (Payment.Status.CANCELLED, Payment.Status.FAILED):
            save_note(f"LATE PAYMENT: M-Pesa {receipt} (KES {paid}) arrived after this payment was cancelled. "
                      "Refund the customer or recreate the order.", with_receipt=True)
        return ok
    if success and receipt and paid >= payment.amount:
        try:
            payment.mark_paid(receipt, "M-Pesa STK payment received")
        except IntegrityError:
            save_note(f"CHECK BY HAND: duplicate M-Pesa receipt {receipt}")
    elif success and receipt:
        save_note(f"UNDERPAID: M-Pesa {receipt} received KES {paid}, expected KES {payment.amount}.", with_receipt=True)
    else:   # cancelled or failed on the phone: leave it open so the customer can retry
        save_note(cb.get("ResultDesc") or "M-Pesa payment not completed")
    return ok
