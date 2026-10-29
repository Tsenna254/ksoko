"""Fixed-window rate limiting on top of Django's cache. Use Redis in production so all workers share counts."""
from django.core.cache import cache
from django.shortcuts import render

from .security import ip_hash


def hit(scope, ident, limit, window):
    """Count one attempt. Returns True while the caller is within the limit."""
    key = f"thr:{scope}:{ident}"
    if cache.add(key, 1, window):
        return True
    try:
        return cache.incr(key) <= limit
    except ValueError:                      # the key expired between add() and incr()
        cache.set(key, 1, window)
        return True


def limited(request, scope, limit, window, ident=None):
    """True when this visitor (by IP hash, or the ident you pass) has made too many attempts."""
    return not hit(scope, ident if ident is not None else ip_hash(request), limit, window)


def too_many(request):
    return render(request, "429.html", status=429)
