"""Search-engine tags for every page: description, canonical address, social-sharing cards and structured data.

Save as market/templatetags/seo_tags.py. base.html calls {% seo_meta %} for every page, and a page can give its own
details by overriding the "seo" block (see the notes in the message that came with this file).
"""
import json
import re
from urllib.parse import urlencode

from django.conf import settings
from django import template
from django.urls import reverse
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import mark_safe

register = template.Library()

KEEP_PARAMS = ("category", "type", "page")          # addresses that are genuinely different pages
PRIVATE_PREFIXES = ("/accounts/", "/cart/", "/checkout/", "/pay/", "/my/", "/manage/", "/orders/", "/sell/")
CONDITIONS = {"new": "NewCondition", "refurbished": "RefurbishedCondition", "used": "UsedCondition"}


def _absolute(request, url):
    if not url:
        return ""
    return url if url.startswith(("http://", "https://")) else request.build_absolute_uri(url)


def _clean(text, limit=160):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def canonical_url(request):
    """The one address a page should be known by: no tracking, sorting or filter noise, only real page changers."""
    params = [(k, request.GET.get(k)) for k in sorted(request.GET) if k in KEEP_PARAMS and request.GET.get(k)]
    params = [(k, v) for k, v in params if not (k == "page" and v == "1")]
    if request.GET.get("q"):                         # search results are their own thing: keep the search words
        params = sorted(params + [("q", request.GET["q"])])
    query = ("?" + urlencode(params)) if params else ""
    return request.build_absolute_uri(request.path) + query


def robots_value(request):
    path = request.path
    if path.startswith(PRIVATE_PREFIXES) or path.startswith("/" + settings.ADMIN_URL):
        return "noindex,nofollow"
    if request.GET.get("q"):
        return "noindex,follow"                     # search results pages shouldn't fill up Google
    return ""


@register.simple_tag(takes_context=True)
def seo_meta(context, title="", description="", image="", og_type="website"):
    request = context.get("request")
    if request is None:
        return ""
    site = context.get("site")
    name = getattr(site, "site_name", "") or "Ksoko"
    description = _clean(description or getattr(site, "hero_subtext", ""))
    title = _clean(title or name, 90)
    if not image and site is not None and getattr(site, "logo", None):
        try:
            image = site.logo.url
        except ValueError:
            image = ""
    image = _absolute(request, image)
    url = canonical_url(request)

    tags = [
        format_html('<meta name="description" content="{}">', description),
        format_html('<link rel="canonical" href="{}">', url),
        format_html('<meta property="og:site_name" content="{}">', name),
        format_html('<meta property="og:type" content="{}">', og_type),
        format_html('<meta property="og:title" content="{}">', title),
        format_html('<meta property="og:description" content="{}">', description),
        format_html('<meta property="og:url" content="{}">', url),
        format_html('<meta name="twitter:card" content="{}">', "summary_large_image" if image else "summary"),
    ]
    if image:
        tags.append(format_html('<meta property="og:image" content="{}">', image))
    robots = robots_value(request)
    if robots:
        tags.append(format_html('<meta name="robots" content="{}">', robots))
    return mark_safe("\n".join(tags))


def _jsonld(data):
    # "<", ">" and "&" are written as escapes, so nothing inside the data can end the script tag early.
    text = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return mark_safe('<script type="application/ld+json">' + text + "</script>")


@register.simple_tag(takes_context=True)
def site_jsonld(context):
    """Who the site is (home page only)."""
    request, site = context.get("request"), context.get("site")
    if request is None:
        return ""
    data = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": getattr(site, "site_name", "Ksoko"), "url": request.build_absolute_uri("/"),
         **({"logo": _absolute(request, site.logo.url)} if site is not None and getattr(site, "logo", None) else {})},
        {"@type": "WebSite", "name": getattr(site, "site_name", "Ksoko"), "url": request.build_absolute_uri("/")},
    ]}
    return _jsonld(data)


def _pick(obj, key):
    """Reads obj[key] or obj.key, so it works with a dictionary or an object."""
    try:
        return obj[key]
    except (TypeError, KeyError, IndexError):
        return getattr(obj, key, None)


@register.simple_tag(takes_context=True)
def product_jsonld(context, listing, rating=None):
    """Lets Google show a product's price, stock and star rating in results. `rating` is optional: anything with
    .n (how many reviews) and .avg (the average)."""
    request = context.get("request")
    if request is None or listing is None:
        return ""
    images = [_absolute(request, i.image.url) for i in listing.images.all()[:8]]
    data = {
        "@context": "https://schema.org", "@type": "Product", "name": listing.title,
        "description": _clean(listing.description, 500), "sku": f"KS-{listing.pk}",
        "url": request.build_absolute_uri(listing.get_absolute_url()),
    }
    if images:
        data["image"] = images
    count = _pick(rating, "n") if rating else None
    average = _pick(rating, "avg") if rating else None
    if count and average:                            # only when real reviews are showing on the page
        data["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": round(float(average), 1),
                                   "reviewCount": int(count)}
    if listing.listing_type == "buy":                # auctions have no fixed price to show
        in_stock = listing.status == "active" and listing.quantity > 0
        data["offers"] = {
            "@type": "Offer", "url": data["url"], "priceCurrency": "KES", "price": f"{listing.price:.2f}",
            "availability": "https://schema.org/" + ("InStock" if in_stock else "OutOfStock"),
            "itemCondition": "https://schema.org/" + CONDITIONS.get(listing.condition, "NewCondition"),
        }
    return _jsonld(data)


@register.simple_tag(takes_context=True)
def breadcrumb_jsonld(context, listing):
    """Home > Category > Sub category > Item, shown under the title in search results."""
    request = context.get("request")
    if request is None or listing is None:
        return ""
    category = listing.category
    crumbs = [("Home", request.build_absolute_uri(reverse("home")))]
    for cat in ([category.parent] if category.parent_id else []) + [category]:
        crumbs.append((cat.name, request.build_absolute_uri(reverse("listing_list") + "?" + urlencode({"category": cat.slug}))))
    crumbs.append((listing.title, request.build_absolute_uri(listing.get_absolute_url())))
    return _jsonld({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": n, "name": text, "item": url} for n, (text, url) in enumerate(crumbs, 1)]})
