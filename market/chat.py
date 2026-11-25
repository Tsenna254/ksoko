"""Order chat: the buyer and seller of an order can message each other once the buyer has paid or chosen Pay on delivery.

It uses plain polling (the page asks for new messages every few seconds), so it needs no WebSockets, no Redis
and no extra services, and it works on Render's free tier.
"""
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import ChatMessage, Order
from .throttle import limited

MAX_LENGTH = 1000     # characters per message
HISTORY = 100         # messages shown when the chat is opened
POLL_BATCH = 100      # most new messages sent back in one poll


def _order_for(user, pk):
    """The order, but only for its buyer or seller and only while chat is open. Everyone else gets a 404."""
    order = get_object_or_404(Order.objects.select_related("listing", "buyer", "seller", "payment"), pk=pk)
    if user.pk not in (order.buyer_id, order.seller_id) or not order.chat_available:
        raise Http404
    return order


def _as_dict(message, user):
    return {
        "id": message.pk,
        "mine": message.sender_id == user.pk,
        "body": message.body,
        "time": timezone.localtime(message.created_at).strftime("%d %b, %H:%M"),
    }


def _mark_read(order, user):
    ChatMessage.objects.filter(order=order, read_at__isnull=True).exclude(sender=user).update(read_at=timezone.now())


@login_required
def order_chat(request, pk):
    order = _order_for(request.user, pk)
    latest = list(order.chat_messages.order_by("-id")[:HISTORY])
    latest.reverse()
    _mark_read(order, request.user)
    is_buyer = request.user.pk == order.buyer_id
    return render(request, "chat/order_chat.html", {
        "order": order,
        "other": order.seller if is_buyer else order.buyer,
        "other_role": "Seller" if is_buyer else "Buyer",
        "back_url": reverse("my_orders" if is_buyer else "seller_sales"),
        "chat_messages": [_as_dict(m, request.user) for m in latest],
        "last_id": latest[-1].pk if latest else 0,
        "max_length": MAX_LENGTH,
    })


@login_required
@require_GET
@never_cache
def chat_messages(request, pk):
    """Returns the messages newer than ?after=<id>. The page calls this every few seconds."""
    order = _order_for(request.user, pk)
    if limited(request, "chat-poll", 120, 60, ident=str(request.user.pk)):
        return JsonResponse({"error": "Slow down."}, status=429)
    try:
        after = int(request.GET.get("after", "0"))
    except ValueError:
        after = 0
    new = list(order.chat_messages.filter(pk__gt=after).order_by("id")[:POLL_BATCH])
    if new:
        _mark_read(order, request.user)
    return JsonResponse({"messages": [_as_dict(m, request.user) for m in new]})


@login_required
@require_POST
def chat_send(request, pk):
    order = _order_for(request.user, pk)
    if limited(request, "chat-send", 20, 60, ident=str(request.user.pk)):
        return JsonResponse({"error": "You're sending messages too fast. Please wait a moment."}, status=429)
    body = request.POST.get("body", "").replace("\r\n", "\n").strip()
    if not body:
        return JsonResponse({"error": "Type a message first."}, status=400)
    if len(body) > MAX_LENGTH:
        return JsonResponse({"error": f"Messages can be up to {MAX_LENGTH} characters."}, status=400)
    message = ChatMessage.objects.create(order=order, sender=request.user, body=body)
    return JsonResponse({"message": _as_dict(message, request.user)})
