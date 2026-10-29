"""Campaign rules and the pages sellers and shoppers use."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.db.models import Avg, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import CampaignJoinForm
from .models import Campaign, CampaignEntry, Listing, Review

OPEN_STATUSES = (CampaignEntry.Status.PENDING, CampaignEntry.Status.APPROVED)


def sync():
    """Start discounts for approved entries in live campaigns and end them everywhere else."""
    now = timezone.now()
    live = Q(status=CampaignEntry.Status.APPROVED, campaign__is_published=True,
             campaign__starts_at__lte=now, campaign__ends_at__gt=now)
    for entry in CampaignEntry.objects.filter(live, applied=False).select_related("campaign"):
        entry.apply()
    for entry in CampaignEntry.objects.filter(applied=True).exclude(live).select_related("campaign"):
        entry.restore()


def reapply(campaign):
    """After staff change a campaign's parameters, re-price its entries with the new settings."""
    for entry in campaign.entries.filter(applied=True):
        entry.restore()
    sync()


def join_blocker(campaign, user):
    """A sentence explaining why this seller can't join, or None if they can."""
    if not campaign.is_joinable:
        return "This campaign isn't open for new entries."
    if campaign.min_seller_rating:
        avg = Review.objects.filter(seller=user).aggregate(a=Avg("seller_rating"))["a"]
        if avg is None or avg < float(campaign.min_seller_rating):
            return f"This campaign needs an average seller rating of at least {campaign.min_seller_rating}."
    return None


def slots_left(campaign, user):
    used = CampaignEntry.objects.filter(campaign=campaign, seller=user, status__in=OPEN_STATUSES).count()
    return max(0, campaign.max_listings_per_seller - used)


def eligible_listings(campaign, user):
    """Active fixed-price listings of this seller that can still join (one running campaign per listing)."""
    qs = Listing.objects.active().filter(seller=user, listing_type=Listing.Type.BUY)
    if campaign.categories.exists():
        qs = qs.filter(category__in=campaign.categories.all())
    busy = CampaignEntry.objects.filter(status__in=OPEN_STATUSES, campaign__ends_at__gt=timezone.now())
    return qs.exclude(pk__in=busy.values("listing_id"))


# ---- Pages --------------------------------------------------------------------

def campaign_detail(request, slug):
    from .views import _listings
    campaign = get_object_or_404(Campaign, slug=slug)
    if not campaign.is_published and not request.user.is_staff:
        raise Http404
    items = _listings().active().filter(campaign_entries__campaign=campaign, campaign_entries__applied=True)
    blocker = join_blocker(campaign, request.user) if request.user.is_authenticated else None
    return render(request, "market/campaign_detail.html", {
        "campaign": campaign, "items": items,
        "can_join": request.user.is_authenticated and blocker is None and slots_left(campaign, request.user) > 0,
        "blocker": blocker,
    })


@login_required
def seller_campaigns(request):
    now = timezone.now()
    open_campaigns = []
    for c in Campaign.objects.filter(is_published=True, ends_at__gt=now).order_by("starts_at"):
        open_campaigns.append({
            "c": c, "blocker": join_blocker(c, request.user), "slots": slots_left(c, request.user),
            "eligible": eligible_listings(c, request.user).count(),
        })
    entries = CampaignEntry.objects.filter(seller=request.user).select_related("campaign", "listing")
    return render(request, "market/seller_campaigns.html", {"open_campaigns": open_campaigns, "entries": entries})


@login_required
def campaign_join(request, slug):
    campaign = get_object_or_404(Campaign, slug=slug, is_published=True)
    blocker = join_blocker(campaign, request.user)
    slots = slots_left(campaign, request.user)
    if blocker or slots < 1:
        messages.error(request, blocker or "You've already added the maximum number of listings to this campaign.")
        return redirect("seller_campaigns")
    eligible = eligible_listings(campaign, request.user)
    form = CampaignJoinForm(eligible, slots, request.POST or None)
    if request.method == "POST" and form.is_valid():
        status = CampaignEntry.Status.PENDING if campaign.requires_approval else CampaignEntry.Status.APPROVED
        with transaction.atomic():
            for listing in form.cleaned_data["listings"]:
                CampaignEntry.objects.create(campaign=campaign, seller=request.user, listing=listing, status=status)
        sync()
        if campaign.requires_approval:
            messages.success(request, "Entries sent. The discount starts once our team approves them and the campaign is live.")
        else:
            messages.success(request, "You're in. Discounts start when the campaign goes live.")
        return redirect("seller_campaigns")
    choices = list(eligible.prefetch_related("images"))
    for listing in choices:
        listing.campaign_price = campaign.discounted(listing.price)
    return render(request, "market/campaign_join.html", {
        "campaign": campaign, "form": form, "choices": choices, "slots": slots,
    })


@login_required
@require_POST
def entry_withdraw(request, pk):
    entry = get_object_or_404(CampaignEntry, pk=pk, seller=request.user)
    if entry.status in OPEN_STATUSES:
        entry.restore()
        entry.status = CampaignEntry.Status.WITHDRAWN
        entry.save(update_fields=["status"])
        messages.info(request, "Your listing was removed from the campaign and its normal price is back.")
    return redirect("seller_campaigns")
