"""Live chat between website visitors and the support team (the Contact us bubble and the staff inbox).

It uses plain polling (pages ask for news every few seconds), so it needs no WebSockets, no Redis and no extra
services, and it works on Render's free tier.

  * Visitors (signed in or not) chat from the bubble. Their conversation is remembered in their session.
  * Staff answer from the inbox at /manage/support/.
  * "Online now" shows while a staff member has the inbox open.
  * When nobody is watching, new visitor messages are emailed to CONTACT_NOTIFY_EMAIL / CONTACT_EMAIL, and a staff
    reply is emailed to a visitor who has left the page, so nobody is left waiting.
"""
import logging
import os
import threading
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db.models import Count, OuterRef, Q, Subquery
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import SupportMessage, SupportStaffSeen, SupportThread
from .throttle import limited

logger = logging.getLogger(__name__)

Sender = SupportMessage.Sender
MAX_LENGTH = 1000                    # characters per message
MAX_MESSAGES = 500                   # messages per conversation
HISTORY = 200                        # most messages sent back at once
STAFF_ONLINE_SECONDS = 90            # staff count as online if their inbox checked in this recently
VISITOR_AWAY_SECONDS = 90            # a visitor counts as gone if their page hasn't checked in this recently
EMAIL_GAP = timedelta(minutes=10)    # at most one alert email per conversation in this time


def _fail(text, status=400):
    return JsonResponse({"error": text}, status=status)


def _limited(request, name, limit, seconds):
    """Rate limit per browser session when there is one (many people share one mobile-network address)."""
    session_key = request.session.session_key
    if session_key:
        return limited(request, name, limit, seconds, ident=session_key)
    return limited(request, name, limit * 4, seconds)


def _after(request):
    try:
        return max(int(request.GET.get("after", "0")), 0)
    except ValueError:
        return 0


def _clock(moment):
    return timezone.localtime(moment).strftime("%d %b, %H:%M")


def _staff_online():
    cutoff = timezone.now() - timedelta(seconds=STAFF_ONLINE_SECONDS)
    return SupportStaffSeen.objects.filter(seen_at__gte=cutoff).exists()


def _support_address():
    return os.environ.get("CONTACT_NOTIFY_EMAIL") or os.environ.get("CONTACT_EMAIL") or getattr(settings, "CONTACT_EMAIL", "")


def _send_in_background(mail):
    def send():
        try:
            mail.send()
        except Exception:
            logger.exception("Support chat email could not be sent")
    threading.Thread(target=send, daemon=True).start()


# ---- Emails for when nobody is watching -------------------------------------------------------

def _alert_staff(request, thread, text):
    to = _support_address()
    now = timezone.now()
    if not to or _staff_online():
        return
    if thread.staff_notified_at and now - thread.staff_notified_at < EMAIL_GAP:
        return
    SupportThread.objects.filter(pk=thread.pk).update(staff_notified_at=now)
    inbox = request.build_absolute_uri(reverse("support_inbox"))
    _send_in_background(EmailMessage(
        subject=f"New Ksoko chat message from {thread.name}",
        body=f"{thread.name} <{thread.email}> wrote:\n\n{text}\n\nAnswer in the live inbox: {inbox}\nOr reply to this email to write to them directly.",
        from_email=settings.DEFAULT_FROM_EMAIL, to=[to], reply_to=[thread.email] if thread.email else None,
    ))


def _email_visitor(thread, text):
    if not thread.email:
        return
    now = timezone.now()
    gone = thread.visitor_seen_at is None or (now - thread.visitor_seen_at).total_seconds() > VISITOR_AWAY_SECONDS
    if not gone or (thread.visitor_emailed_at and now - thread.visitor_emailed_at < EMAIL_GAP):
        return
    SupportThread.objects.filter(pk=thread.pk).update(visitor_emailed_at=now)
    support = _support_address()
    _send_in_background(EmailMessage(
        subject="Ksoko support replied to your message",
        body=f"Hi {thread.name},\n\n{text}\n\nReply to this email to carry on the conversation.\n\nKsoko support",
        from_email=settings.DEFAULT_FROM_EMAIL, to=[thread.email], reply_to=[support] if support else None,
    ))


# ---- The visitor's side (the bubble) ----------------------------------------------------------

def _visitor_thread(request):
    pk = request.session.get("support_thread")
    thread = SupportThread.objects.filter(pk=pk).first() if pk else None
    if thread is None and request.user.is_authenticated:
        thread = SupportThread.objects.filter(user=request.user).order_by("-last_message_at").first()
        if thread:
            request.session["support_thread"] = thread.pk
    return thread


def _visitor_view(message):
    mine = message.sender == Sender.VISITOR
    return {"id": message.pk, "mine": mine, "who": "You" if mine else "Ksoko Support",
            "body": message.body, "time": _clock(message.created_at)}


@require_POST
@never_cache
def visitor_send(request):
    if _limited(request, "support-send", 30, 60):
        return _fail("You're sending messages too fast. Please wait a moment.", 429)
    if request.POST.get("website"):                    # hidden trap field: people never see it, spam bots fill it in
        return JsonResponse({"ok": True})
    body = request.POST.get("body", "").replace("\r\n", "\n").strip()
    if not body:
        return _fail("Type a message first.")
    if len(body) > MAX_LENGTH:
        return _fail(f"Messages can be up to {MAX_LENGTH} characters.")

    thread = _visitor_thread(request)
    if thread is None:
        if limited(request, "support-new", 20, 3600):
            return _fail("Too many new chats from your network. Please try again later, or use WhatsApp.", 429)
        user = request.user if request.user.is_authenticated else None
        name = " ".join(request.POST.get("name", "").split())[:80] or (user.get_username() if user else "")
        email = request.POST.get("email", "").strip()[:254] or (user.email if user else "")
        if not name:
            return _fail("Please enter your name.")
        try:
            validate_email(email)
        except ValidationError:
            return _fail("Please enter a valid email address, so we can reach you if you leave.")
        thread = SupportThread.objects.create(user=user, name=name, email=email)
        request.session["support_thread"] = thread.pk
    elif thread.messages.count() >= MAX_MESSAGES:
        return _fail("This chat is full. Please email us instead.")

    message = SupportMessage.objects.create(thread=thread, sender=Sender.VISITOR, body=body)
    now = timezone.now()
    SupportThread.objects.filter(pk=thread.pk).update(last_message_at=now, is_open=True, visitor_seen_at=now)
    _alert_staff(request, thread, body)
    return JsonResponse({"message": _visitor_view(message), "staff_online": _staff_online()})


@require_GET
@never_cache
def visitor_poll(request):
    """Called every few seconds by the bubble. With ?open=1 it returns messages newer than ?after=<id>."""
    if _limited(request, "support-poll", 150, 60):
        return JsonResponse({"error": "Slow down."}, status=429)
    online = _staff_online()
    thread = _visitor_thread(request)
    if thread is None:
        return JsonResponse({"has_thread": False, "staff_online": online, "unread": 0, "messages": []})

    now = timezone.now()
    if thread.visitor_seen_at is None or (now - thread.visitor_seen_at).total_seconds() > 20:
        SupportThread.objects.filter(pk=thread.pk).update(visitor_seen_at=now)
    staff_messages = thread.messages.filter(sender=Sender.STAFF, read_at__isnull=True)
    data = {"has_thread": True, "staff_online": online, "unread": staff_messages.count(), "messages": []}
    if request.GET.get("open") == "1":
        new = thread.messages.filter(pk__gt=_after(request)).order_by("id")[:HISTORY]
        data["messages"] = [_visitor_view(m) for m in new]
        staff_messages.update(read_at=now)            # the window is open, so they are being read
        data["unread"] = 0
    return JsonResponse(data)


# ---- The staff inbox --------------------------------------------------------------------------

def staff_only(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise Http404
        return view(request, *args, **kwargs)
    return wrapper


def _staff_view(message):
    if message.sender == Sender.STAFF:
        who = message.staff.get_username() if message.staff_id else "Support"
    else:
        who = message.thread.name
    return {"id": message.pk, "mine": message.sender == Sender.STAFF, "who": who,
            "body": message.body, "time": _clock(message.created_at)}


@staff_only
def inbox(request):
    return render(request, "contact/inbox.html")


@staff_only
@require_GET
@never_cache
def staff_threads(request):
    now = timezone.now()
    SupportStaffSeen.objects.update_or_create(user=request.user, defaults={"seen_at": now})
    last_body = SupportMessage.objects.filter(thread=OuterRef("pk")).order_by("-id").values("body")[:1]
    threads = SupportThread.objects.select_related("user").annotate(
        unread=Count("messages", filter=Q(messages__sender=Sender.VISITOR, messages__read_at__isnull=True)),
        preview=Subquery(last_body),
    )
    if request.GET.get("closed") != "1":
        threads = threads.filter(is_open=True)
    rows = [{
        "id": t.pk, "name": t.name, "email": t.email, "is_open": t.is_open, "unread": t.unread,
        "preview": (t.preview or "")[:70], "time": _clock(t.last_message_at),
    } for t in threads.order_by("-last_message_at")[:100]]
    return JsonResponse({"threads": rows, "unread": sum(r["unread"] for r in rows)})


@staff_only
@require_GET
@never_cache
def staff_messages(request, pk):
    thread = get_object_or_404(SupportThread, pk=pk)
    now = timezone.now()
    new = thread.messages.filter(pk__gt=_after(request)).select_related("staff", "thread").order_by("id")[:HISTORY]
    thread.messages.filter(sender=Sender.VISITOR, read_at__isnull=True).update(read_at=now)
    visitor_here = bool(thread.visitor_seen_at and (now - thread.visitor_seen_at).total_seconds() <= VISITOR_AWAY_SECONDS)
    return JsonResponse({
        "messages": [_staff_view(m) for m in new],
        "thread": {"id": thread.pk, "name": thread.name, "email": thread.email, "is_open": thread.is_open,
                   "signed_in_as": thread.user.get_username() if thread.user_id else "", "visitor_here": visitor_here},
    })


@staff_only
@require_POST
@never_cache
def staff_reply(request, pk):
    thread = get_object_or_404(SupportThread, pk=pk)
    if limited(request, "support-reply", 60, 60, ident=str(request.user.pk)):
        return _fail("Slow down a little.", 429)
    body = request.POST.get("body", "").replace("\r\n", "\n").strip()
    if not body:
        return _fail("Type a reply first.")
    if len(body) > MAX_LENGTH:
        return _fail(f"Replies can be up to {MAX_LENGTH} characters.")
    message = SupportMessage.objects.create(thread=thread, sender=Sender.STAFF, staff=request.user, body=body)
    SupportThread.objects.filter(pk=thread.pk).update(last_message_at=timezone.now(), is_open=True)
    _email_visitor(thread, body)
    return JsonResponse({"message": _staff_view(message)})


@staff_only
@require_POST
@never_cache
def staff_toggle(request, pk):
    thread = get_object_or_404(SupportThread, pk=pk)
    thread.is_open = not thread.is_open
    thread.save(update_fields=["is_open"])
    return JsonResponse({"is_open": thread.is_open})
