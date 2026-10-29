"""Housekeeping that runs by itself (see MaintenanceMiddleware) or from `manage.py run_maintenance`."""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from . import campaigns, emailer
from .models import OutboundEmail, PageView, Payment, SearchEvent, VerificationCode


def run_minutely():
    campaigns.sync()                                           # start and end campaign discounts
    emailer.process_outbox()                                   # send or retry queued emails
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
    call_command("clearsessions")
