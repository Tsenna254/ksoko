"""Registration and sign-in with emailed verification codes.

  * New accounts must confirm their email address with a code before they can sign in.
  * Signing in from a network (IP address) the account hasn't used before needs a fresh code.
  * Staff accounts need a code on every sign-in.
Nobody is logged in until the code is accepted, so a stolen password alone is not enough.
"""
import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import PasswordResetConfirmView, PasswordResetView
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import audit, verification
from .forms import QueuedPasswordResetForm, RegisterForm
from .models import AccountSecurity, KnownIP, SiteSettings
from .security import ip_hash, mask_email, privacy_hash
from .throttle import limited, too_many
from .verification import Purpose

PENDING_SECONDS = 15 * 60
MAX_KNOWN_IPS = 20


def safe_next(request):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    ok = nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return nxt if ok else ""


def _announce(request, user, status):
    if status == "sent":
        messages.success(request, f"We're sending a 6-digit code to {mask_email(user.email)}. It works for {settings.CODE_TTL_MINUTES} minutes.")
    elif status == "wait":
        messages.info(request, "A code was just sent. Please wait a minute before asking for another.")
    elif status == "limit":
        messages.error(request, "Too many codes were requested. Please try again in an hour.")
    else:
        messages.error(request, "We couldn't send the email just now. Please try again in a moment.")


def _start(request, user, purpose, nxt):
    request.session["pending"] = {"uid": user.pk, "purpose": purpose, "next": nxt, "t": time.time()}
    _announce(request, user, verification.issue_code(request, user, purpose))


def _finish_login(request, user):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    KnownIP.objects.update_or_create(user=user, ip_hash=ip_hash(request), defaults={"last_seen": timezone.now()})
    for old in user.known_ips.order_by("-last_seen")[MAX_KNOWN_IPS:]:
        old.delete()
    if user.is_staff:
        request.session.set_expiry(settings.STAFF_SESSION_SECONDS)
        audit.log(request, "staff.login", user.username)


def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    nxt = safe_next(request)
    form = RegisterForm(request.POST or None)
    if request.method == "POST":
        if limited(request, "register", 5, 3600):
            return too_many(request)
        if form.is_valid():
            user = form.save()
            AccountSecurity.objects.get_or_create(user=user)
            _start(request, user, Purpose.REGISTER, nxt)
            return redirect("verify")
    return render(request, "registration/register.html", {"form": form, "next": nxt})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")
    nxt = safe_next(request)
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST":
        name = privacy_hash("user", request.POST.get("username", "").strip().lower()[:150])
        if limited(request, "login-ip", 15, 300) or limited(request, "login-user", 8, 600, ident=name):
            messages.error(request, "Too many sign-in attempts. Please wait a few minutes and try again.")
            return render(request, "registration/login.html", {"form": AuthenticationForm(request), "next": nxt}, status=429)
        if form.is_valid():
            return _begin_login(request, form.get_user(), nxt)
    return render(request, "registration/login.html", {"form": form, "next": nxt})


def _begin_login(request, user, nxt):
    security, _ = AccountSecurity.objects.get_or_create(user=user)
    if not user.email:
        messages.error(request, "This account has no email address, so it can't be verified. Staff can add one with: python manage.py set_email USERNAME EMAIL")
        return redirect("login")
    if not security.email_verified:
        purpose = Purpose.REGISTER
    elif user.is_staff or not KnownIP.objects.filter(user=user, ip_hash=ip_hash(request)).exists():
        purpose = Purpose.LOGIN                       # staff every time, everyone else from a new network
    else:
        _finish_login(request, user)
        return redirect(nxt or "home")
    _start(request, user, purpose, nxt)
    return redirect("verify")


def _pending(request):
    pending = request.session.get("pending")
    if not pending or time.time() - pending.get("t", 0) > PENDING_SECONDS:
        request.session.pop("pending", None)
        return None, None
    return pending, get_user_model().objects.filter(pk=pending["uid"]).first()


def verify(request):
    pending, user = _pending(request)
    if user is None:
        messages.info(request, "Your verification session ended. Please sign in again.")
        return redirect("login")
    purpose, error = pending["purpose"], None
    if request.method == "POST":
        if limited(request, "verify", 15, 600):
            return too_many(request)
        result = verification.check_code(request, user, purpose, request.POST.get("code", ""))
        if result == "ok":
            if purpose == Purpose.REGISTER:
                AccountSecurity.objects.update_or_create(user=user, defaults={"email_verified": True, "verified_at": timezone.now()})
            nxt = pending.get("next", "")
            request.session.pop("pending", None)
            _finish_login(request, user)
            messages.success(request, "Email confirmed. Welcome to Ksoko!" if purpose == Purpose.REGISTER else "Verified. You're signed in.")
            return redirect(nxt or "home")
        error = {
            "invalid": "That code isn't right. Check the email and try again.",
            "expired": "That code has expired. Ask for a new one below.",
            "locked": "Too many wrong tries for this code. Ask for a new one below.",
            "network": "This code was sent for a different network. Sign in again to get a new one.",
        }[result]
    return render(request, "registration/verify.html", {
        "email": mask_email(user.email), "error": error, "purpose": purpose, "minutes": settings.CODE_TTL_MINUTES,
        "is_register": purpose == Purpose.REGISTER,
        "delivery": verification.delivery_status(user, purpose),
    })


@require_POST
def resend(request):
    pending, user = _pending(request)
    if user is None:
        return redirect("login")
    if limited(request, "resend", 10, 3600):
        return too_many(request)
    _announce(request, user, verification.issue_code(request, user, pending["purpose"]))
    return redirect("verify")


def admin_login_redirect(request):
    """The Django admin's own login page would skip the email checks, so send everyone through ours."""
    nxt = request.GET.get("next") or reverse("admin:index")
    return redirect(f"{reverse('login')}?{urlencode({'next': nxt})}")


# ---- Forgotten password ----------------------------------------------------------------------

class PasswordResetRequestView(PasswordResetView):
    """Step 1: the visitor gives their email address and we send a one-time link.

    The reply is identical whether or not the address belongs to an account, so this page can't be used
    to find out who has an account. Requests are rate limited per network and per email address.
    """
    template_name = "registration/pw_reset_form.html"
    form_class = QueuedPasswordResetForm
    email_template_name = "emails/pw_reset_email.txt"
    html_email_template_name = "emails/pw_reset_email.html"
    subject_template_name = "emails/pw_reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")

    def form_valid(self, form):
        email = form.cleaned_data["email"].strip().lower()
        if limited(self.request, "pwreset-ip", 5, 3600):
            return too_many(self.request)
        if limited(self.request, "pwreset-email", 3, 3600, ident=privacy_hash("email", email)):
            return HttpResponseRedirect(self.get_success_url())        # quietly send nothing
        self.extra_email_context = {"brand": SiteSettings.load().site_name, "minutes": settings.PASSWORD_RESET_TIMEOUT // 60}
        return super().form_valid(form)


class PasswordResetNewPasswordView(PasswordResetConfirmView):
    """Step 2: the emailed link opens this page, where the visitor picks a new password."""
    template_name = "registration/pw_reset_confirm.html"
    success_url = reverse_lazy("password_reset_complete")

    def dispatch(self, request, *args, **kwargs):
        if limited(request, "pwreset-confirm", 30, 3600):
            return too_many(request)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        user.known_ips.all().delete()                  # after a reset, every network must prove itself again with a code
        audit.log(self.request, "password.reset", user.username)
        return response
