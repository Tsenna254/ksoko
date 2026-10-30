from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import CartItem, Listing, Order


def cart_summary(user):
    """Everything a customer owes right now: cart lines plus auctions they have won."""
    items = CartItem.objects.filter(user=user).select_related("listing__seller").prefetch_related("listing__images", "listing__fees")
    lines, subtotal, delivery = [], Decimal("0"), Decimal("0")
    for item in items:
        listing = item.listing
        ok = (listing.listing_type == Listing.Type.BUY and listing.is_open
              and listing.quantity >= item.quantity and listing.seller_id != user.id)
        line = listing.price * item.quantity
        lines.append({"item": item, "listing": listing, "qty": item.quantity, "line": line,
                      "fee": listing.delivery_fee, "ok": ok})
        if ok:
            subtotal += line
            delivery += listing.delivery_fee
    unpaid = list(Order.objects.filter(buyer=user, status=Order.Status.UNPAID, payment__isnull=True))
    won_total = sum((o.total for o in unpaid), Decimal("0"))
    return {
        "lines": lines, "unpaid": unpaid, "subtotal": subtotal, "delivery": delivery, "won_total": won_total,
        "total": subtotal + delivery + won_total,
        "count": sum(l["qty"] for l in lines) + len(unpaid),
        "blocked": any(not l["ok"] for l in lines),
    }


def _safe_next(request):
    nxt = request.POST.get("next", "")
    return nxt if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) else None


@login_required
def cart_view(request):
    return render(request, "market/cart.html", {"cart": cart_summary(request.user)})


@login_required
@require_POST
def cart_add(request, pk):
    listing = get_object_or_404(Listing, pk=pk, listing_type=Listing.Type.BUY)
    if listing.seller_id == request.user.id:
        messages.error(request, "You can't buy your own listing.")
    elif not listing.is_open or listing.quantity < 1:
        messages.error(request, "Sorry, this item is sold out.")
    else:
        item, created = CartItem.objects.get_or_create(user=request.user, listing=listing)
        if not created:
            if item.quantity < listing.quantity:
                item.quantity += 1
                item.save(update_fields=["quantity"])
            else:
                messages.info(request, "Your cart already has all the available stock of this item.")
                return redirect("cart")
        if request.POST.get("buy_now"):
            return redirect("checkout")
        messages.success(request, f"Added to your cart: {listing.title}")
    return redirect(_safe_next(request) or listing)


@login_required
@require_POST
def cart_update(request, pk):
    item = get_object_or_404(CartItem.objects.select_related("listing"), pk=pk, user=request.user)
    try:
        qty = int(request.POST.get("quantity", "1"))
    except ValueError:
        qty = item.quantity
    if qty <= 0:
        item.delete()
    else:
        item.quantity = max(1, min(qty, item.listing.quantity))
        item.save(update_fields=["quantity"])
    return redirect("cart")


@login_required
@require_POST
def cart_remove(request, pk):
    get_object_or_404(CartItem, pk=pk, user=request.user).delete()
    return redirect("cart")
