from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDay, TruncHour, TruncMonth
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from . import audit
from .security import mask_email
from .models import Listing, ListingFee, OutboundEmail, Order, PageView, Payment, Review, SearchEvent

# key: (label, truncation function, window description)
PERIODS = {
    "day": ("Daily", TruncHour, "last 24 hours, by hour"),
    "week": ("Weekly", TruncDay, "last 7 days, by day"),
    "month": ("Monthly", TruncDay, "last 30 days, by day"),
    "year": ("Annually", TruncMonth, "last 12 months, by month"),
}


def _buckets(period, now):
    """Return (bucket start times, label format, label every N bars)."""
    now = timezone.localtime(now)
    if period == "day":
        end = now.replace(minute=0, second=0, microsecond=0)
        return [end - timedelta(hours=i) for i in range(23, -1, -1)], "%H:00", 3
    if period in ("week", "month"):
        n = 7 if period == "week" else 30
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return ([end - timedelta(days=i) for i in range(n - 1, -1, -1)],
                "%a %d" if period == "week" else "%d %b", 1 if period == "week" else 5)
    out = []
    for i in range(11, -1, -1):
        year, month = now.year, now.month - i
        while month < 1:
            month += 12
            year -= 1
        out.append(now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0))
    return out, "%b %y", 1


def _bars(buckets, fmt, every, data):
    values = [float(data.get(b) or 0) for b in buckets]
    peak = max(values + [0]) or 1
    return [
        {"label": b.strftime(fmt), "show": i % every == 0, "value": values[i], "pct": round(values[i] / peak * 100)}
        for i, b in enumerate(buckets)
    ]


def _delta(cur, prev):
    cur, prev = float(cur or 0), float(prev or 0)
    if not prev:
        return None
    pct = round((cur - prev) / prev * 100)
    return {"pct": abs(pct), "up": pct >= 0}


@staff_member_required
def dashboard(request):
    period = request.GET.get("period", "week")
    if period not in PERIODS:
        period = "week"
    label, trunc, span = PERIODS[period]

    now = timezone.now()
    buckets, fmt, every = _buckets(period, now)
    start = buckets[0]
    prev_start = start - (now - start)

    def span_qs(model, field, prev=False):
        base = model.objects if hasattr(model, "objects") else model
        if prev:
            return base.filter(**{field + "__gte": prev_start, field + "__lt": start})
        return base.filter(**{field + "__gte": start})

    views, views_p = span_qs(PageView, "created_at"), span_qs(PageView, "created_at", True)
    done = Order.objects.filter(status=Order.Status.COMPLETED)  # only payments staff have confirmed count as sales
    orders, orders_p = span_qs(done, "completed_at"), span_qs(done, "completed_at", True)
    User = get_user_model()

    visitors, visitors_p = (q.order_by().values("session_key").distinct().count() for q in (views, views_p))
    o, op = (q.aggregate(n=Count("id"), rev=Sum("total")) for q in (orders, orders_p))
    revenue, revenue_p = o["rev"] or 0, op["rev"] or 0
    aov = revenue / o["n"] if o["n"] else 0
    aov_p = revenue_p / op["n"] if op["n"] else 0
    paid_fees = ListingFee.objects.filter(status=ListingFee.Status.PAID)
    fees = span_qs(paid_fees, "paid_at").aggregate(t=Sum("amount"))["t"] or 0
    fees_p = span_qs(paid_fees, "paid_at", True).aggregate(t=Sum("amount"))["t"] or 0
    new_listings, new_listings_p = span_qs(Listing, "created_at").count(), span_qs(Listing, "created_at", True).count()
    new_users, new_users_p = span_qs(User, "date_joined").count(), span_qs(User, "date_joined", True).count()
    ratings = Review.objects.aggregate(product=Avg("product_rating"), seller=Avg("seller_rating"), n=Count("id"))

    kpis = [
        {"label": "Sales", "value": revenue, "money": True, "delta": _delta(revenue, revenue_p)},
        {"label": "Transactions", "value": o["n"], "delta": _delta(o["n"], op["n"])},
        {"label": "Pay on Delivery fees", "value": fees, "money": True, "delta": _delta(fees, fees_p)},
        {"label": "Average order", "value": aov, "money": True, "delta": _delta(aov, aov_p)},
        {"label": "Page views", "value": views.count(), "delta": _delta(views.count(), views_p.count())},
        {"label": "Unique visitors", "value": visitors, "delta": _delta(visitors, visitors_p)},
        {"label": "New listings", "value": new_listings, "delta": _delta(new_listings, new_listings_p)},
        {"label": "New members", "value": new_users, "delta": _delta(new_users, new_users_p)},
        {"label": "Avg vendor rating", "value": ratings["seller"], "rating": True, "note": f"{ratings['n']} review(s), all time"},
    ]

    def series(qs, field, **aggs):
        rows = qs.annotate(b=trunc(field)).values("b").annotate(**aggs).order_by()
        return rows

    view_rows = {r["b"]: r["v"] for r in series(views, "created_at", v=Count("id"))}
    rev_rows, cnt_rows = {}, {}
    for r in series(orders, "completed_at", rev=Sum("total"), n=Count("id")):
        rev_rows[r["b"]], cnt_rows[r["b"]] = r["rev"], r["n"]
    charts = [
        {"title": "Page views", "bars": _bars(buckets, fmt, every, view_rows)},
        {"title": "Sales (KES)", "bars": _bars(buckets, fmt, every, rev_rows), "money": True},
        {"title": "Transactions", "bars": _bars(buckets, fmt, every, cnt_rows)},
    ]

    vendor_q = User.objects.annotate(avg=Avg("reviews_received__seller_rating"), n=Count("reviews_received")).filter(n__gt=0)
    product_q = Listing.objects.annotate(avg=Avg("reviews__product_rating"), n=Count("reviews")).filter(n__gt=0)

    return render(request, "market/dashboard.html", {
        "period": period, "periods": [(k, v[0]) for k, v in PERIODS.items()], "label": label, "span": span,
        "kpis": kpis, "charts": charts,
        "top_pages": views.values("path").annotate(c=Count("id")).order_by("-c")[:8],
        "top_viewed": views.exclude(listing=None).values("listing_id", "listing__title").annotate(c=Count("id")).order_by("-c")[:8],
        "email_stats": _email_stats(),
        "attention_payments": Payment.objects.filter(
            status__in=[Payment.Status.CANCELLED, Payment.Status.FAILED], provider_ref__gt="").select_related("user")[:10],
        "top_searches": span_qs(SearchEvent, "created_at").values("query").annotate(c=Count("id")).order_by("-c")[:8],
        "no_result_searches": span_qs(SearchEvent, "created_at").filter(results=0).values("query").annotate(c=Count("id")).order_by("-c")[:8],
        "top_vendors": orders.values("seller__username").annotate(rev=Sum("total"), n=Count("id")).order_by("-rev")[:5],
        "recent_orders": done.select_related("buyer", "seller")[:8],
        "pending_payments": sorted(
            Payment.objects.filter(status__in=[Payment.Status.PAID, Payment.Status.AWAITING, Payment.Status.COD_PENDING]).select_related("user"),
            key=lambda p: (p.status != Payment.Status.PAID, -p.created_at.timestamp()),
        ),
        "rating_groups": [
            ("Best rated vendors", vendor_q.order_by("-avg", "-n")[:5], "vendor"),
            ("Lowest rated vendors", vendor_q.order_by("avg", "-n")[:5], "vendor"),
            ("Best rated products", product_q.order_by("-avg", "-n")[:5], "product"),
            ("Lowest rated products", product_q.order_by("avg", "-n")[:5], "product"),
        ],
    })


@staff_member_required
@require_POST
def payment_decision(request, pk, action):
    payment = get_object_or_404(Payment, pk=pk)
    reason = request.POST.get("reason", "").strip()
    if action == "confirm":
        if payment.confirm(request.user, manual_reason=reason):
            audit.log(request, "payment.confirm", payment.reference, reason or "customer had paid; verified by staff")
            messages.success(request, f"Payment {payment.reference} confirmed. The orders are now complete.")
        else:
            messages.error(request, "Only payments the customer has paid can be confirmed. To confirm an unpaid one, give a reason.")
    elif action == "reject" and payment.release(Payment.Status.REJECTED, f"Rejected by {request.user.username}"):
        audit.log(request, "payment.reject", payment.reference, reason)
        messages.warning(request, f"Payment {payment.reference} rejected and stock returned. Refund the customer if money was received.")
    else:
        messages.error(request, "That payment can't be changed any more.")
    return redirect("dashboard")


def _email_stats():
    """Delivery health over the last 24 hours, so a broken email setup is noticed straight away."""
    since = timezone.now() - timedelta(hours=24)
    recent = OutboundEmail.objects.filter(created_at__gte=since)
    counts = {row["status"]: row["n"] for row in recent.values("status").annotate(n=Count("id"))}
    problems = [
        {"when": m.created_at, "kind": m.get_kind_display(), "to": mask_email(m.to_email), "status": m.get_status_display(), "error": m.last_error}
        for m in recent.filter(Q(status=OutboundEmail.Status.FAILED) | Q(last_error__gt="")).order_by("-created_at")[:5]
    ]
    return {"counts": counts, "total": sum(counts.values()), "failed": counts.get("failed", 0), "problems": problems}
