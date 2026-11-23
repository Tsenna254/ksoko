"""Builds the verification-code emails and hands them to the queue (market/emailer.py)."""
from datetime import timedelta

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from . import emailer
from .models import OutboundEmail, SiteSettings, VerificationCode
from .security import client_ip, mask_ip

Purpose = VerificationCode.Purpose
SUBJECTS = {Purpose.REGISTER: "Confirm your email address", Purpose.LOGIN: "Your sign-in code"}
KINDS = {Purpose.REGISTER: OutboundEmail.Kind.REGISTER_CODE, Purpose.LOGIN: OutboundEmail.Kind.LOGIN_CODE}


def queue_code_email(request, user, code, purpose):
    site = SiteSettings.load().site_name
    ctx = {
        "user": user, "code": code, "purpose": purpose, "site": site, "minutes": settings.CODE_TTL_MINUTES,
        "ip": mask_ip(client_ip(request)), "when": timezone.localtime().strftime("%d %b %Y, %H:%M"),
        "is_register": purpose == Purpose.REGISTER,
    }
    return emailer.queue_email(
        kind=KINDS[purpose], to_email=user.email, subject=f"{site}: {SUBJECTS[purpose]}",
        text=render_to_string("emails/verification.txt", ctx), html=render_to_string("emails/verification.html", ctx),
        user=user, expires_at=timezone.now() + timedelta(minutes=settings.CODE_TTL_MINUTES),
    )
