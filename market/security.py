"""Small helpers shared by the security features. No database access here."""
import hashlib
import hmac
import os

from django.conf import settings


def privacy_hash(namespace, value):
    """Keyed hash used instead of storing raw IP addresses or session keys."""
    digest = hmac.new(settings.IP_HASH_SALT.encode(), f"{namespace}:{value}".encode(), hashlib.sha256)
    return digest.hexdigest()[:40]


def client_ip(request):
    if settings.TRUST_PROXY_HEADERS:                       # only behind a proxy you control
        # Each proxy appends the address it saw, so count from the right: the left side is whatever the visitor
        # claimed and can be faked. TRUSTED_PROXY_COUNT is how many proxies sit between visitors and the site.
        hops = [h.strip() for h in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if h.strip()]
        if hops:
            return hops[-min(settings.TRUSTED_PROXY_COUNT, len(hops))]
    return request.META.get("REMOTE_ADDR", "")


def ip_hash(request):
    ip = client_ip(request)
    return privacy_hash("ip", ip) if ip else ""


def mask_email(email):
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}" if domain else "your email address"


def mask_ip(ip):
    """197.232.14.9 -> 197.232.*.*  (enough for the owner to recognise, not enough to locate)."""
    if ":" in ip:
        return ":".join(ip.split(":")[:2]) + ":*"
    parts = ip.split(".")
    return ".".join(parts[:2] + ["*", "*"]) if len(parts) == 4 else "an unknown network"


def safe_ext(filename, allowed, default):
    """Only ever save uploads with an extension we choose, so a file can't be served as HTML or script."""
    ext = os.path.splitext(filename)[1].lower()
    return ext if ext in allowed else default
