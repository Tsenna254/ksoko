"""The filter pop-up and sort menu on listing pages.

Built-in filters: price, condition, format, free delivery, Pay on Delivery, on sale.
Admin-defined filters (FilterParameter): choices, number ranges and yes/no, shown for the category being browsed.

URL names:  min_price max_price cond type free_delivery cod on_sale
            p<ID>=<option id>  (repeat for several)   p<ID>_min / p<ID>_max   p<ID>=1
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.db.models import Case, DecimalField, F, OuterRef, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce

from .models import Category, FilterParameter, Listing, ListingFee

SORTS = [
    ("new", "Newest"), ("low", "Price: low to high"), ("high", "Price: high to low"),
    ("ending", "Ending soonest"), ("bids", "Most bids"), ("rated", "Top rated"), ("discount", "Biggest discount"),
]
NAMES_BY_KEY = dict(SORTS)
BASIC_KEYS = ("min_price", "max_price", "cond", "type", "free_delivery", "cod", "on_sale")


def load_parameters():
    return list(FilterParameter.objects.filter(is_active=True, show_in_filter=True).prefetch_related("options", "categories"))


def _dec(text):
    try:
        value = Decimal(str(text).strip())
        return value if value >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def apply(qs, GET, specs):
    """Narrow the listings by everything chosen in the filter menu."""
    lo, hi = _dec(GET.get("min_price", "")), _dec(GET.get("max_price", ""))
    if lo is not None:
        qs = qs.filter(price__gte=lo)
    if hi is not None:
        qs = qs.filter(price__lte=hi)
    conditions = [c for c in GET.getlist("cond") if c in Listing.Condition.values]
    if conditions:
        qs = qs.filter(condition__in=conditions)
    if GET.get("type") in Listing.Type.values:
        qs = qs.filter(listing_type=GET["type"])
    if GET.get("free_delivery") == "1":
        qs = qs.filter(delivery_fee=0)
    if GET.get("on_sale") == "1":
        qs = qs.filter(listing_type=Listing.Type.BUY, original_price__gt=F("price"))
    if GET.get("cod") == "1":      # opted in, and the seller's fee covers the item's value (same rule as Listing.cod_active)
        paid = (ListingFee.objects.filter(listing=OuterRef("pk"), status="paid").order_by().values("listing")
                .annotate(total=Sum("covers_value")).values("total"))
        qs = (qs.filter(allow_cod=True, listing_type=Listing.Type.BUY)
              .annotate(cod_cover=Coalesce(Subquery(paid), Value(Decimal("0")), output_field=DecimalField()))
              .filter(cod_cover__gte=F("price") * F("quantity")))
    for p in specs:
        key = f"p{p.pk}"
        if p.kind == FilterParameter.Kind.CHOICE:
            ids = [int(x) for x in GET.getlist(key) if x.isdigit()]
            if ids:
                qs = qs.filter(attributes__parameter=p, attributes__option_id__in=ids)
        elif p.kind == FilterParameter.Kind.NUMBER:
            lo, hi = _dec(GET.get(key + "_min", "")), _dec(GET.get(key + "_max", ""))
            bounds = {}
            if lo is not None:
                bounds["attributes__number__gte"] = lo
            if hi is not None:
                bounds["attributes__number__lte"] = hi
            if bounds:              # one filter() call so both limits apply to the same value
                qs = qs.filter(attributes__parameter=p, **bounds)
        elif GET.get(key) == "1":
            qs = qs.filter(attributes__parameter=p, attributes__yes=True)
    return qs


def sort(qs, key):
    if key == "low":
        return qs.order_by("price", "-created_at")
    if key == "high":
        return qs.order_by("-price", "-created_at")
    if key == "ending":
        return qs.order_by(F("ends_at").asc(nulls_last=True), "-created_at")
    if key == "bids":
        return qs.order_by("-num_bids", "-created_at")
    if key == "rated":
        return qs.order_by(F("product_avg").desc(nulls_last=True), "-num_reviews", "-created_at")
    if key == "discount":
        percent = Case(
            When(original_price__gt=F("price"), then=(F("original_price") - F("price")) * 100 / F("original_price")),
            default=Value(0), output_field=DecimalField(max_digits=8, decimal_places=2),
        )
        return qs.annotate(discount_pct=percent).order_by("-discount_pct", "-created_at")
    return qs.order_by("-created_at")


def _url(GET, drop=(), **set_values):
    """The current page's address with some filters removed or changed (and back on page 1)."""
    params = GET.copy()
    params.pop("page", None)
    for item in drop:
        key, value = item if isinstance(item, tuple) else (item, None)
        if value is None:
            params.pop(key, None)
        else:
            kept = [v for v in params.getlist(key) if v != value]
            if kept:
                params.setlist(key, kept)
            else:
                params.pop(key, None)
    for key, value in set_values.items():
        params[key] = value
    encoded = params.urlencode()
    return "?" + encoded if encoded else "?"


def toolbar(request, current, specs):
    """Everything the filter pop-up, the active-filter chips and the sort menu need."""
    GET = request.GET
    shown = [p for p in specs if p.applies_to(current)]
    options = {o.pk: o for p in specs for o in p.options.all()}

    sections, chips = [], []
    lo, hi = GET.get("min_price", ""), GET.get("max_price", "")
    if _dec(lo) is not None or _dec(hi) is not None:
        chips.append({"label": f"Price: KES {lo or '0'} to {hi or 'any'}", "url": _url(GET, drop=("min_price", "max_price"))})
    for c in GET.getlist("cond"):
        if c in Listing.Condition.values:
            chips.append({"label": Listing.Condition(c).label, "url": _url(GET, drop=[("cond", c)])})
    if GET.get("type") in Listing.Type.values:
        chips.append({"label": Listing.Type(GET["type"]).label, "url": _url(GET, drop=("type",))})
    for key, label in (("free_delivery", "Free delivery"), ("cod", "Pay on Delivery"), ("on_sale", "On sale")):
        if GET.get(key) == "1":
            chips.append({"label": label, "url": _url(GET, drop=(key,))})

    for p in specs:
        key = f"p{p.pk}"
        if p.kind == FilterParameter.Kind.CHOICE:
            chosen = {int(x) for x in GET.getlist(key) if x.isdigit()}
            for oid in sorted(chosen):
                if oid in options and options[oid].parameter_id == p.pk:
                    chips.append({"label": f"{p.name}: {options[oid].value}", "url": _url(GET, drop=[(key, str(oid))])})
        elif p.kind == FilterParameter.Kind.NUMBER:
            a, b = GET.get(key + "_min", ""), GET.get(key + "_max", "")
            if _dec(a) is not None or _dec(b) is not None:
                chips.append({"label": f"{p.name}: {a or 'any'} to {b or 'any'} {p.unit}".strip(),
                              "url": _url(GET, drop=(key + "_min", key + "_max"))})
        elif GET.get(key) == "1":
            chips.append({"label": f"{p.name}: Yes", "url": _url(GET, drop=(key,))})

    for p in shown:
        key = f"p{p.pk}"
        chosen = {x for x in GET.getlist(key)}
        sections.append({
            "param": p, "key": key, "kind": p.kind,
            "options": [{"id": o.pk, "value": o.value, "checked": str(o.pk) in chosen} for o in p.options.all()],
            "min": GET.get(key + "_min", ""), "max": GET.get(key + "_max", ""), "yes": GET.get(key) == "1",
        })

    keep = [(k, GET[k]) for k in ("q", "category", "sort") if GET.get(k)]
    sort_key = GET.get("sort") if GET.get("sort") in NAMES_BY_KEY else "new"
    return {
        "chips": chips, "filter_count": len(chips), "filter_sections": sections, "keep": keep,
        "fv": {"min_price": lo, "max_price": hi, "cond": set(GET.getlist("cond")), "type": GET.get("type", ""),
               "free_delivery": GET.get("free_delivery") == "1", "cod": GET.get("cod") == "1", "on_sale": GET.get("on_sale") == "1"},
        "conditions": Listing.Condition.choices,
        "clear_url": "?" + urlencode([(k, v) for k, v in keep if k != "sort"]) if any(k != "sort" for k, _ in keep) else "?",
        "sort_links": [{"key": k, "label": label, "url": _url(GET, sort=k), "active": k == sort_key} for k, label in SORTS],
        "sort_label": NAMES_BY_KEY[sort_key],
        "top_categories": Category.objects.top() if current is None else [],
    }
