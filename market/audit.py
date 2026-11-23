from .models import AuditLog
from .security import ip_hash


def log(request, action, target="", detail=""):
    """Record who did something sensitive (confirming payments, staff sign-ins, branding changes)."""
    AuditLog.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        action=action, target=str(target)[:200], detail=str(detail)[:300], ip_hash=ip_hash(request),
    )
