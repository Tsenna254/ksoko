"""Personalised suggestions built from what a visitor searches for and looks at.

Who is "the visitor"?
  1. A logged-in user is tracked by user id.
  2. A guest is tracked by an anonymous visitor id kept in their session (it survives logging in).
  3. The IP address is only a weak fallback for a guest with almost no history. It is stored as a salted
     hash (never the raw address), counts for a quarter as much, and only borrows *anonymous* history.
     Shared networks (offices, mobile carriers) put many people behind one IP, so it is never trusted on its own.

How a suggestion is chosen
  1. Collect signals: searches (3), cart (3), purchases (4), watchlist (2) and page views (1), each fading with a
     14-day half-life so recent interest outweighs old interest.
  2. Turn them into an interest profile: weighted keywords, weighted categories and a typical price.
  3. Pull candidate listings that match a keyword or a favourite category (never the visitor's own items,
     or things already in their cart or bought).
  4. Score each candidate:
        0.50 keyword match (title counts fully, description 40%)   0.20 category affinity
        0.10 price fit to what they usually look at                0.08 seller and product ratings
        0.07 popularity in the last 14 days                        0.05 freshness
     Items they have already viewed are scaled down to 60%.
  5. Diversify (at most 4 per category and 2 per seller), then label each with the reason it was picked.
  Visitors with no history get what is trending instead.
"""
import hashlib
import hmac
import math
import re
import uuid
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import CartItem, Listing, Order, PageView, SearchEvent, Watch
from .security import client_ip, ip_hash
from .throttle import limited

BOTS = re.compile(r"bot|crawl|spider|slurp|preview|monitor|curl|wget|python-requests", re.I)

HALF_LIFE_DAYS = 14
WEIGHTS = {"search": 3.0, "view": 1.0, "watch": 2.0, "cart": 3.0, "buy": 4.0, "seed": 5.0}
IP_WEIGHT = 0.25
MIN_OWN_SIGNALS = 3       # below this, a guest may borrow anonymous history from the same IP
STOP_WORDS = {
    "the", "and", "for", "with", "new", "used", "good", "best", "cheap", "sale", "buy", "item", "items", "from",
    "this", "that", "your", "you", "are", "was", "has", "have", "one", "set", "pcs", "pair", "very", "size",
}


# ---- Who is this visitor? ---------------------------------------------------------

def visitor_id(request):
    return request.session.setdefault("vid", uuid.uuid4().hex)


def personalisation_off(request):
    return bool(request.session.get("no_personalise"))


def _own(model, request):
    """Rows (SearchEvent or PageView) that belong to this user or this guest."""
    q = Q(user=request.user) if request.user.is_authenticated else None
    vid = request.session.get("vid")
    if vid:
        q = (q | Q(visitor_id=vid)) if q else Q(visitor_id=vid)
    return model.objects.filter(q) if q else model.objects.none()


# ---- Recording --------------------------------------------------------------------

def record_search(request, query, category, results):
    if personalisation_off(request) or BOTS.search(request.META.get("HTTP_USER_AGENT", "")):
        return
    if limited(request, "searchlog", 30, 60):
        return
    text = " ".join(query.lower().split())[:100]
    if len(text) < 2:
        return
    vid = visitor_id(request)
    mine = Q(visitor_id=vid) | (Q(user=request.user) if request.user.is_authenticated else Q())
    if SearchEvent.objects.filter(mine, query=text, created_at__gte=timezone.now() - timedelta(minutes=5)).exists():
        return                                              # same search repeated or paged: count it once
    SearchEvent.objects.create(
        user=request.user if request.user.is_authenticated else None,
        visitor_id=vid, ip_hash=ip_hash(request), query=text, category=category, results=results,
    )


def recent_queries(request, limit=6):
    seen, out = set(), []
    for e in _own(SearchEvent, request).order_by("-created_at")[:40]:
        if e.query not in seen:
            seen.add(e.query)
            out.append(e.query)
        if len(out) == limit:
            break
    return out


# ---- Building the interest profile ---------------------------------------------------

def tokens(text):
    """Lower-case keywords, with a light plural fix so 'phones' matches 'phone'."""
    out = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) < 3 or word in STOP_WORDS or word.isdigit():
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return out


class Profile:
    def __init__(self):
        self.terms, self.search_terms, self.cats = Counter(), Counter(), Counter()
        self.prices, self.seen, self.excluded = [], set(), set()
        self.signals = 0

    def add_text(self, text, weight, from_search=False):
        for t in tokens(text):
            self.terms[t] += weight
            if from_search:
                self.search_terms[t] += weight

    def add_listing(self, listing, weight):
        self.cats[listing.category_id] += weight
        self.add_text(listing.title, weight * 0.5)
        self.prices.append((float(listing.price), weight))

    def typical_price(self):
        """Weighted median of the prices they have looked at."""
        if not self.prices:
            return None
        total, acc = sum(w for _, w in self.prices), 0.0
        for price, w in sorted(self.prices):
            acc += w
            if acc >= total / 2:
                return price


def build_profile(request, seed=None):
    now = timezone.now()
    since = now - timedelta(days=settings.SEARCH_HISTORY_DAYS)
    p = Profile()

    def fade(ts):
        return 0.5 ** (max(0.0, (now - ts).total_seconds()) / 86400 / HALF_LIFE_DAYS)

    if not personalisation_off(request):
        own = list(_own(SearchEvent, request).filter(created_at__gte=since).select_related("category")[:200])
        for e in own:
            w = WEIGHTS["search"] * fade(e.created_at)
            p.add_text(e.query, w, from_search=True)
            if e.category_id:
                p.cats[e.category_id] += w
            p.signals += 1

        views = _own(PageView, request).filter(listing__isnull=False, created_at__gte=since).select_related("listing")[:200]
        for v in views:
            p.add_listing(v.listing, WEIGHTS["view"] * fade(v.created_at))
            p.seen.add(v.listing_id)
            p.signals += 1

        if request.user.is_authenticated:
            for w in Watch.objects.filter(user=request.user).select_related("listing"):
                p.add_listing(w.listing, WEIGHTS["watch"] * fade(w.created_at))
                p.signals += 1
            for c in CartItem.objects.filter(user=request.user).select_related("listing"):
                p.add_listing(c.listing, WEIGHTS["cart"])
                p.excluded.add(c.listing_id)
                p.signals += 1
            for o in Order.objects.filter(buyer=request.user).exclude(status=Order.Status.CANCELLED).select_related("listing"):
                p.add_listing(o.listing, WEIGHTS["buy"] * fade(o.created_at))
                p.excluded.add(o.listing_id)
                p.signals += 1

        if p.signals < MIN_OWN_SIGNALS:        # weak fallback: borrow anonymous searches from the same IP
            h = ip_hash(request)
            if h:
                borrowed = (SearchEvent.objects.filter(ip_hash=h, user__isnull=True, created_at__gte=since)
                            .exclude(visitor_id=request.session.get("vid", "")).order_by("-created_at")[:30])
                for e in borrowed:
                    p.add_text(e.query, WEIGHTS["search"] * IP_WEIGHT * fade(e.created_at), from_search=True)
                    if e.category_id:
                        p.cats[e.category_id] += WEIGHTS["search"] * IP_WEIGHT * fade(e.created_at)
                    p.signals += 0.5

    if seed is not None:                         # "more like this item"
        p.add_listing(seed, WEIGHTS["seed"])
        p.excluded.add(seed.pk)
        p.signals += 1
    return p


# ---- Finding and ranking candidates -----------------------------------------------------

def _base(request):
    qs = Listing.objects.active().select_related("category", "seller").prefetch_related("images", "campaign_entries__campaign", "fees")
    if request.user.is_authenticated:
        qs = qs.exclude(seller=request.user)
    return qs.with_counts().annotate(
        recent_views=Count("views", filter=Q(views__created_at__gte=timezone.now() - timedelta(days=14)), distinct=True)
    )


def trending(request, limit, exclude=()):
    items = list(_base(request).exclude(pk__in=list(exclude)).order_by("-recent_views", "-created_at")[:limit])
    for l in items:
        l.rec_reason = "Trending now"
    return items


def _diversify(scored, limit, per_category=4, per_seller=2):
    out, rest, cats, sellers = [], [], Counter(), Counter()
    for _, listing in scored:
        if cats[listing.category_id] >= per_category or sellers[listing.seller_id] >= per_seller:
            rest.append(listing)
            continue
        out.append(listing)
        cats[listing.category_id] += 1
        sellers[listing.seller_id] += 1
        if len(out) == limit:
            return out
    return out + rest[: limit - len(out)]


def recommend_for(request, limit=8, seed=None):
    """Return (listings, mode). mode is "personal" when history shaped the list, else "trending"."""
    p = build_profile(request, seed)
    if p.signals <= 0 or not (p.terms or p.cats):
        return trending(request, limit, p.excluded), "trending"

    now = timezone.now()
    top_terms = p.terms.most_common(12)
    term_total = sum(w for _, w in top_terms) or 1.0
    max_cat = max(p.cats.values()) if p.cats else 1.0
    typical = p.typical_price()

    match = Q(category_id__in=[c for c, _ in p.cats.most_common(3)])
    for term, _ in top_terms[:8]:
        match |= Q(title__icontains=term) | Q(description__icontains=term)
    candidates = list(_base(request).exclude(pk__in=list(p.excluded)).filter(match)[:300])
    max_recent = max([c.recent_views for c in candidates] + [1])

    scored = []
    for l in candidates:
        title_words, desc_words = set(tokens(l.title)), set(tokens(l.description))
        text, matched = 0.0, []
        for term, w in top_terms:
            if term in title_words:
                text += w
                matched.append((w, term))
            elif term in desc_words:
                text += w * 0.4
                matched.append((w * 0.4, term))
        text /= term_total
        category = p.cats.get(l.category_id, 0.0) / max_cat
        fit = math.exp(-abs(math.log(float(l.price) / typical))) if typical and l.price > 0 else 0.0
        seller_q = (float(l.seller_avg) / 5) if l.seller_avg else 0.5      # unrated sellers are neutral
        product_q = (float(l.product_avg) / 5) if l.product_avg else 0.5
        popularity = math.log1p(l.recent_views) / math.log1p(max_recent)
        fresh = 0.5 ** (max(0.0, (now - l.created_at).total_seconds()) / 86400 / 30)
        score = 0.50 * text + 0.20 * category + 0.10 * fit + 0.08 * (seller_q + product_q) / 2 + 0.07 * popularity + 0.05 * fresh
        if l.pk in p.seen:
            score *= 0.6

        if matched:
            term = max(matched)[1]
            l.rec_reason = (f"Because you searched “{term}”" if term in p.search_terms
                            else "Similar to this item" if seed is not None else "Based on items you viewed")
        elif category > 0:
            l.rec_reason = f"More in {l.category.name}"
        else:
            l.rec_reason = "Popular right now"
        scored.append((score, l))

    scored.sort(key=lambda s: s[0], reverse=True)
    picks = _diversify(scored, limit)
    if len(picks) < limit:                                   # top up with what is trending
        picks += trending(request, limit - len(picks), p.excluded | {l.pk for l in picks})
    return picks, "personal"


# ---- Privacy controls ---------------------------------------------------------------------

@require_POST
def personalisation(request, action):
    """clear = forget my history; off = also stop recording; on = switch it back on."""
    if action in ("clear", "off"):
        _own(SearchEvent, request).delete()
        _own(PageView, request).update(user=None, visitor_id="")   # keep traffic totals, drop the link to this person
        messages.success(request, "Your search and browsing history was cleared.")
    if action == "off":
        request.session["no_personalise"] = True
        messages.info(request, "Personalised suggestions are off. You'll see what's trending instead.")
    elif action == "on":
        request.session.pop("no_personalise", None)
        messages.success(request, "Personalised suggestions are on.")
    nxt = request.POST.get("next", "")
    ok = nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
    return redirect(nxt if ok else "home")
