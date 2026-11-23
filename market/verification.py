"""Emailed six-digit codes: issuing them, checking them, and knowing who is verified."""
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .emails import queue_code_email
from .models import AccountSecurity, OutboundEmail, VerificationCode
from .security import ip_hash, privacy_hash

Purpose = VerificationCode.Purpose


def _hash(user_id, purpose, code):
    return privacy_hash("code", f"{user_id}:{purpose}:{code}")


def is_email_verified(user):
    cached = getattr(user, "_email_verified", None)
    if cached is None:
        cached = AccountSecurity.objects.filter(user=user, email_verified=True).exists()
        user._email_verified = cached
    return cached


def issue_code(request, user, purpose):
    """Create a new code and queue its email. Returns "sent", "wait", "limit" or "error"."""
    now = timezone.now()
    codes = VerificationCode.objects.filter(user=user, purpose=purpose)
    last = codes.order_by("-created_at").first()
    if last and (now - last.created_at).total_seconds() < settings.CODE_RESEND_SECONDS:
        return "wait"
    if codes.filter(created_at__gte=now - timedelta(hours=1)).count() >= settings.CODE_MAX_PER_HOUR:
        return "limit"
    code = f"{secrets.randbelow(10 ** 6):06d}"
    with transaction.atomic():
        codes.filter(used_at__isnull=True).update(used_at=now)     # older codes stop working
        record = VerificationCode.objects.create(
            user=user, purpose=purpose, code_hash=_hash(user.pk, purpose, code),
            ip_hash=ip_hash(request) if purpose == Purpose.LOGIN else "",
            expires_at=now + timedelta(minutes=settings.CODE_TTL_MINUTES),
        )
    try:
        queue_code_email(request, user, code, purpose)
    except Exception:
        record.delete()
        return "error"
    return "sent"


@transaction.atomic
def check_code(request, user, purpose, submitted):
    """Returns "ok", "invalid", "expired", "locked" or "network"."""
    submitted = "".join(ch for ch in submitted if ch.isdigit())
    now = timezone.now()
    record = (VerificationCode.objects.select_for_update()
              .filter(user=user, purpose=purpose, used_at__isnull=True, expires_at__gt=now)
              .order_by("-created_at").first())
    if record is None:
        return "expired"
    if record.attempts >= settings.CODE_MAX_ATTEMPTS:
        return "locked"
    record.attempts += 1
    record.save(update_fields=["attempts"])
    if purpose == Purpose.LOGIN and record.ip_hash != ip_hash(request):
        return "network"
    if len(submitted) == 6 and hmac.compare_digest(record.code_hash, _hash(user.pk, purpose, submitted)):
        record.used_at = now
        record.save(update_fields=["used_at"])
        return "ok"
    return "invalid"


DELIVERY_KIND = {Purpose.REGISTER: OutboundEmail.Kind.REGISTER_CODE, Purpose.LOGIN: OutboundEmail.Kind.LOGIN_CODE}


def delivery_status(user, purpose):
    """Where the most recent code email for this user stands (queued, sending, sent, failed ...), or None."""
    mail = OutboundEmail.objects.filter(user=user, kind=DELIVERY_KIND[purpose]).order_by("-created_at").first()
    return mail.status if mail else None
