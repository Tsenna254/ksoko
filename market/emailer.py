"""The email queue.

Every verification code and password-reset email goes through here:

  queue_email()   saves the email and starts sending it in the background, so the page answers instantly
  process_outbox() sends what is waiting; failures are retried after 30 s, 2 min, 5 min, 15 min, then marked failed
  A new code replaces older waiting ones, and a code that has already expired is never sent.

It runs by itself: a background thread right after queueing, a sweep every minute from the site, and optionally a
dedicated worker (`python manage.py send_queued_emails --loop`). Any number of them can run at once safely.
"""
import threading
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import OutboundEmail

S = OutboundEmail.Status


def retry_delay(attempts):
    """Seconds to wait after the given number of failed attempts."""
    delays = settings.EMAIL_RETRY_SECONDS
    return delays[min(max(attempts, 1), len(delays)) - 1]


def queue_email(kind, to_email, subject, text, html="", user=None, expires_at=None):
    if user is not None:      # a fresh email replaces any older one of the same kind that hasn't gone out yet
        OutboundEmail.objects.filter(user=user, kind=kind, status=S.QUEUED).update(status=S.CANCELLED, body_text="", body_html="")
    mail = OutboundEmail.objects.create(
        user=user, kind=kind, to_email=to_email, subject=subject[:200], body_text=text, body_html=html, expires_at=expires_at,
    )
    transaction.on_commit(flush_async)
    return mail


def flush_async():
    if not settings.EMAIL_SEND_ASYNC:
        process_outbox()
        return
    threading.Thread(target=_run_in_thread, daemon=True).start()


def _run_in_thread():
    try:
        process_outbox()
    except Exception:
        pass              # the once-a-minute sweep picks up anything left behind
    finally:
        close_old_connections()


def _finish(mail, status, error=""):
    """Record the outcome and wipe the message text (it held a code or link)."""
    mail.status, mail.body_text, mail.body_html = status, "", ""
    if error:
        mail.last_error = error[:300]
    mail.save(update_fields=["status", "body_text", "body_html", "last_error", "attempts", "sent_at"])


def process_outbox(limit=None):
    """Send what is due. Returns how many emails went out."""
    limit = limit or settings.EMAIL_SEND_BATCH
    now = timezone.now()
    OutboundEmail.objects.filter(status=S.SENDING, next_attempt_at__lt=now - timedelta(minutes=5)).update(status=S.QUEUED)  # crashed worker
    due = OutboundEmail.objects.filter(status=S.QUEUED, next_attempt_at__lte=now).order_by("next_attempt_at")
    sent = 0
    for pk in list(due.values_list("pk", flat=True)[:limit]):
        # Claim it so two workers never send the same email.
        if not OutboundEmail.objects.filter(pk=pk, status=S.QUEUED).update(status=S.SENDING, next_attempt_at=timezone.now()):
            continue
        mail = OutboundEmail.objects.get(pk=pk)
        if mail.expires_at and mail.expires_at <= timezone.now():
            _finish(mail, S.EXPIRED, "Expired before it could be sent")
            continue
        try:
            message = EmailMultiAlternatives(mail.subject, mail.body_text, settings.DEFAULT_FROM_EMAIL, [mail.to_email])
            if mail.body_html:
                message.attach_alternative(mail.body_html, "text/html")
            message.send(fail_silently=False)
        except Exception as exc:
            mail.attempts += 1
            error = f"{type(exc).__name__}: {exc}"
            if mail.attempts >= settings.EMAIL_MAX_ATTEMPTS:
                _finish(mail, S.FAILED, error)
            else:
                mail.status, mail.last_error = S.QUEUED, error[:300]
                mail.next_attempt_at = timezone.now() + timedelta(seconds=retry_delay(mail.attempts))
                mail.save(update_fields=["status", "last_error", "attempts", "next_attempt_at"])
        else:
            mail.attempts += 1
            mail.sent_at = timezone.now()
            _finish(mail, S.SENT)
            sent += 1
    return sent
