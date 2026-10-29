from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .throttle import limited, too_many
from .recommend import recent_queries, record_search, recommend_for, personalisation_off
from .forms import BidForm, ListingForm, RegisterForm, ReviewForm
from .models import Bid, Campaign, Category, Listing, MediaAsset, Order, Review, Watch


def _limits():
    return {"max_images": settings.MAX_LISTING_IMAGES, "max_mb": settings.MAX_IMAGE_MB}


def _listings():
    return Listing.objects.select_related("category", "seller").prefetch_related("images", "campaign_entries__campaign").with_counts()


def home(request):
    live = _listings().active()
    recs, rec_mode = recommend_for(request, 8)
    return render(request, "market/home.html", {
        "deals": live.filter(listing_type=Listing.Type.BUY, original_price__isnull=False)[:8],
        "auctions": live.filter(listing_type=Listing.Type.AUCTION).order_by("ends_at")[:4],
        "recent": live[:8],
        "categories": Category.objects.all(),
        "banners": MediaAsset.objects.filter(is_active=True, placement=MediaAsset.Placement.HOME)[:3],
        "campaigns": Campaign.objects.filter(is_published=True, featured=True, ends_at__gt=timezone.now()).order_by("starts_at")[:3],
        "recs": recs, "rec_mode": rec_mode,
        "recent_searches": recent_queries(request),
        "personalise_on": not personalisation_off(request),
    })


def listing_list(request):
    qs = _listings().active()
    q = request.GET.get("q", "").strip()
    if q and limited(request, "search", 60, 60):
        return too_many(request)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    slug = request.GET.get("category", "")
    if slug:
        qs = qs.filter(category__slug=slug)
    kind = request.GET.get("type", "")
    if kind in Listing.Type.values:
        qs = qs.filter(listing_type=kind)
    sorts = {
        "new": "-created_at",
        "low": "price",
        "high": "-price",
        "ending": F("ends_at").asc(nulls_last=True),
    }
    sort = request.GET.get("sort", "new")
    qs = qs.order_by(sorts.get(sort, "-created_at"))

    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    if q and request.GET.get("page") in (None, "", "1"):
        record_search(request, q, Category.objects.filter(slug=slug).first() if slug else None, page.paginator.count)
    suggested = recommend_for(request, 4)[0] if not page.object_list else []
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "market/listing_list.html", {
        "page": page, "q": q, "slug": slug, "kind": kind, "sort": sort,
        "querystring": params.urlencode(),
        "suggested": suggested,
        "current_category": Category.objects.filter(slug=slug).first() if slug else None,
        "list_banner": MediaAsset.objects.filter(is_active=True, placement=MediaAsset.Placement.LISTINGS).first(),
    })


def listing_detail(request, pk):
    listing = get_object_or_404(
        Listing.objects.select_related("seller", "category").prefetch_related("images"), pk=pk
    )
    user = request.user
    is_seller = user.is_authenticated and listing.seller_id == user.id
    return render(request, "market/listing_detail.html", {
        "listing": listing,
        "is_seller": is_seller,
        "can_bid": listing.is_auction and listing.is_open and not is_seller,
        "can_buy": not listing.is_auction and listing.is_open and not is_seller,
        "bid_form": BidForm(initial={"amount": listing.min_next_bid}),
        "bids": listing.bids.select_related("bidder")[:5],
        "bid_count": listing.bids.count(),
        "watching": user.is_authenticated and Watch.objects.filter(user=user, listing=listing).exists(),
        "similar": recommend_for(request, 4, seed=listing)[0],
        "reviews": listing.reviews.select_related("reviewer")[:10],
        "product_rating": listing.reviews.aggregate(avg=Avg("product_rating"), n=Count("id")),
        "seller_rating": Review.objects.filter(seller=listing.seller).aggregate(avg=Avg("seller_rating"), n=Count("id")),
        "won_order": (Order.objects.filter(listing=listing, buyer=user, status=Order.Status.UNPAID).first()
                      if user.is_authenticated else None),
        "order_to_rate": (Order.objects.filter(listing=listing, buyer=user, status=Order.Status.COMPLETED, review__isnull=True).first()
                          if user.is_authenticated else None),
    })


# ---- Customer uploads: creating, editing and deleting listings -------------

@login_required
def create_listing(request):
    # request.FILES carries the uploaded photos; the form validates them.
    form = ListingForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        listing = form.save(seller=request.user)
        messages.success(request, "Your listing is live.")
        return redirect(listing)
    return render(request, "market/listing_form.html", {"form": form, "listing": None, **_limits()})


@login_required
def edit_listing(request, pk):
    listing = get_object_or_404(Listing, pk=pk, seller=request.user)
    if listing.status != Listing.Status.ACTIVE:
        messages.error(request, "Sold or ended listings can't be edited.")
        return redirect(listing)
    form = ListingForm(request.POST or None, request.FILES or None, instance=listing)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Listing updated.")
        return redirect(listing)
    return render(request, "market/listing_form.html", {"form": form, "listing": listing, **_limits()})


@login_required
def delete_listing(request, pk):
    listing = get_object_or_404(Listing, pk=pk, seller=request.user)
    if listing.bids.exists() or listing.orders.exists():
        messages.error(request, "Listings with bids or sales can't be deleted.")
        return redirect(listing)
    if request.method == "POST":
        listing.delete()  # photo files are removed by the post_delete signal
        messages.success(request, "Listing deleted.")
        return redirect("my_listings")
    return render(request, "market/listing_confirm_delete.html", {"listing": listing})


@login_required
def my_listings(request):
    items = _listings().filter(seller=request.user).annotate(num_views=Count("views", distinct=True))
    return render(request, "market/my_listings.html", {"items": items})


# ---- Buying, bidding and watching ------------------------------------------

@login_required
@require_POST
def place_bid(request, pk):
    form = BidForm(request.POST)
    if limited(request, "bid", 30, 60, ident=request.user.pk):
        messages.error(request, "You're bidding too fast. Wait a moment and try again.")
        return redirect("listing_detail", pk=pk)
    with transaction.atomic():
        listing = get_object_or_404(Listing.objects.select_for_update(), pk=pk, listing_type=Listing.Type.AUCTION)
        error = None
        if listing.seller_id == request.user.id:
            error = "You can't bid on your own listing."
        elif not listing.is_open:
            error = "This auction has ended."
        elif not form.is_valid():
            error = "Enter a valid bid amount."
        elif form.cleaned_data["amount"] < listing.min_next_bid:
            error = f"Your bid must be at least KES {listing.min_next_bid:,.0f}."
        if error:
            messages.error(request, error)
        else:
            amount = form.cleaned_data["amount"]
            Bid.objects.create(listing=listing, bidder=request.user, amount=amount)
            listing.price = amount
            listing.save(update_fields=["price", "updated_at"])
            messages.success(request, f"You're the highest bidder at KES {amount:,.0f}.")
    return redirect("listing_detail", pk=pk)


@login_required
@require_POST
def toggle_watch(request, pk):
    listing = get_object_or_404(Listing, pk=pk)
    watch, created = Watch.objects.get_or_create(user=request.user, listing=listing)
    if not created:
        watch.delete()
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(listing)


@login_required
def watchlist(request):
    ids = Watch.objects.filter(user=request.user).values_list("listing_id", flat=True)
    return render(request, "market/watchlist.html", {"items": _listings().filter(pk__in=ids)})





# ---- Orders, ratings and vendor profiles -----------------------------------

@login_required
def my_orders(request):
    orders = Order.objects.filter(buyer=request.user).select_related("listing", "seller", "review", "payment")
    return render(request, "market/my_orders.html", {"orders": orders})


@login_required
def review_create(request, pk):
    order = get_object_or_404(Order.objects.select_related("listing", "seller"), pk=pk, buyer=request.user)
    if order.status != Order.Status.COMPLETED:
        messages.info(request, "You can rate an order once your payment has been confirmed.")
        return redirect("my_orders")
    if Review.objects.filter(order=order).exists():
        messages.info(request, "You've already rated this order.")
        return redirect(order.listing)
    form = ReviewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        review = form.save(commit=False)
        review.order, review.listing, review.seller, review.reviewer = order, order.listing, order.seller, request.user
        review.save()
        messages.success(request, "Thanks, your rating has been posted.")
        return redirect(order.listing)
    return render(request, "market/review_form.html", {"form": form, "order": order})


def seller_profile(request, username):
    seller = get_object_or_404(get_user_model(), username=username)
    return render(request, "market/seller_profile.html", {
        "seller": seller,
        "rating": Review.objects.filter(seller=seller).aggregate(avg=Avg("seller_rating"), n=Count("id")),
        "sales_count": Order.objects.filter(seller=seller).count(),
        "items": _listings().active().filter(seller=seller)[:12],
        "reviews": Review.objects.filter(seller=seller).select_related("reviewer", "listing")[:10],
    })
