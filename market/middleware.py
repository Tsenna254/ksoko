from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.core.cache import cache
from django.shortcuts import redirect

from . import maintenance
from .models import PageView
from .recommend import BOTS, visitor_id
from .security import privacy_hash
from .throttle import limited
from .verification import is_email_verified

SKIP = ("/static/", "/media/", "/favicon.ico")


class AccountGuardMiddleware:
    """Signed-in users must have a verified email, and staff sessions end after a period of inactivity."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not request.path.startswith(SKIP):
            if not is_email_verified(user):
                logout(request)
                messages.error(request, "Please confirm your email address first. Sign in again and we'll send you a code.")
                return redirect("login")
            if user.is_staff:
                request.session.set_expiry(settings.STAFF_SESSION_SECONDS)
        return self.get_response(request)


class TrafficMiddleware:
    """Records one PageView per successful HTML page load, for the admin dashboard."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self.record(request, response)
        except Exception:  # analytics must never break the site
            pass
        return response

    def record(self, request, response):
        if request.method != "GET" or response.status_code != 200:
            return
        admin_path = "/" + settings.ADMIN_URL
        if request.path.startswith(SKIP + (admin_path, "/manage/")) or request.user.is_staff:
            return
        if not response.get("Content-Type", "").startswith("text/html"):
            return
        if BOTS.search(request.META.get("HTTP_USER_AGENT", "")):
            return
        if limited(request, "pageview", 120, 60):        # a flood from one address can't fill the database
            return
        if not request.session.session_key:
            request.session.save()
        match = request.resolver_match
        listing_id = match.kwargs.get("pk") if match and match.url_name == "listing_detail" else None
        PageView.objects.create(
            path=request.path[:300],
            user=request.user if request.user.is_authenticated else None,
            session_key=privacy_hash("sess", request.session.session_key or ""),   # a hash, never the real session key
            visitor_id=visitor_id(request),
            listing_id=listing_id,
        )


class MaintenanceMiddleware:
    """Runs housekeeping at most once a minute (and once an hour) without needing cron."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        for key, ttl, task in (("maint-minute", 60, maintenance.run_minutely), ("maint-hour", 3600, maintenance.run_hourly)):
            if cache.add(key, 1, ttl):
                try:
                    task()
                except Exception:
                    pass
        return self.get_response(request)
