"""Housekeeping that runs by itself (see MaintenanceMiddleware) or from `manage.py run_maintenance`."""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connections, transaction
from django.utils import timezone

from . import campaigns, emailer, gateways
from .models import OutboundEmail, PageView, Payment, SearchEvent, SupportThread, VerificationCode

logger = logging.getLogger(__name__)


def reconcile_mpesa():
    """Safaricom's confirmation can go missing (for example while a free host is asleep). Ask Safaricom about prompts
    that are still unanswered and record the ones that were paid. Live (production) only, because the sandbox's
    answers can't be relied on. Staff still confirm every payment against the M-Pesa statement."""
    if settings.MPESA_ENV != "production" or not gateways.mpesa_stk_ready():
        return
    waiting = (Payment.objects
               .filter(method=Payment.Method.MPESA, status=Payment.Status.AWAITING,
                       created_at__lt=timezone.now() - timedelta(seconds=90))
               .exclude(mpesa_checkout_id="").order_by("-created_at")[:5])
    for payment in waiting:
        try:
            state = gateways.mpesa_stk_query(payment.mpesa_checkout_id)
        except gateways.GatewayError:
            continue
        if state != "paid":
            continue                                           # failed or still pending: leave it to the callback and the expiry
        with transaction.atomic():
            fresh = Payment.objects.select_for_update().get(pk=payment.pk)
            if fresh.status == Payment.Status.AWAITING:
                fresh.mark_paid(f"QUERY-{fresh.reference}",
                                "Safaricom says this was paid but its confirmation never arrived. "
                                "Find the real M-Pesa code in the statement before confirming.")
                logger.warning("Payment %s recorded as paid from a Safaricom status query", fresh.reference)


def _reconcile_in_background():
    """Runs off the visitor's request, so nobody waits for Safaricom."""
    def work():
        try:
            reconcile_mpesa()
        except Exception:
            logger.exception("M-Pesa reconciliation failed")
        finally:
            connections.close_all()                            # this thread's own database connections
    threading.Thread(target=work, daemon=True).start()


def run_minutely():
    # Emails go first and each step is isolated, so one failing task can't stop verification codes from being sent.
    try:
        emailer.process_outbox()                               # send or retry queued emails
    except Exception:
        logger.exception("Sending queued emails failed")
    try:
        campaigns.sync()                                       # start and end campaign discounts
    except Exception:
        logger.exception("Campaign sync failed")
    _reconcile_in_background()                                 # catch payments whose confirmation never arrived
    cutoff = timezone.now() - timedelta(minutes=settings.PAYMENT_HOLD_MINUTES)
    for payment in Payment.objects.filter(status=Payment.Status.AWAITING, created_at__lt=cutoff):
        payment.release(Payment.Status.CANCELLED, "Expired: no payment received")   # returns the stock


def run_hourly():
    now = timezone.now()
    VerificationCode.objects.filter(created_at__lt=now - timedelta(days=1)).delete()
    OutboundEmail.objects.filter(created_at__lt=now - timedelta(days=settings.EMAIL_OUTBOX_RETENTION_DAYS)).delete()
    get_user_model().objects.filter(
        is_staff=False, is_superuser=False, last_login__isnull=True,
        date_joined__lt=now - timedelta(days=1), security__email_verified=False,
    ).delete()                                                 # sign-ups that never confirmed their email
    PageView.objects.filter(created_at__lt=now - timedelta(days=settings.PAGEVIEW_RETENTION_DAYS)).delete()
    SearchEvent.objects.filter(created_at__lt=now - timedelta(days=settings.SEARCH_HISTORY_DAYS)).delete()
    SupportThread.objects.filter(last_message_at__lt=now - timedelta(days=getattr(settings, "SUPPORT_RETENTION_DAYS", 90))).delete()
    call_command("clearsessions")
