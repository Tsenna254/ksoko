"""Staff-only pages for branding, promotional media and campaigns."""
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import audit, campaigns
from .forms import CampaignForm, MediaForm, SiteSettingsForm
from .models import Campaign, CampaignEntry, MediaAsset, SiteSettings


@staff_member_required
def branding(request):
    form = SiteSettingsForm(request.POST or None, request.FILES or None, instance=SiteSettings.load())
    if request.method == "POST":
        if form.is_valid():
            changed = [form.fields[n].label or n.replace("_", " ") for n in form.changed_data]
            form.save()
            audit.log(request, "branding.update", "Site settings", ", ".join(changed))
            if changed:
                messages.success(request, "Saved and live on the site: " + ", ".join(c.lower() for c in changed) + ".")
            else:
                messages.info(request, "Nothing was changed. Choose a new file or edit a field first.")
            return redirect("manage_branding")
        messages.error(request, "Your changes were NOT saved. Fix the problems marked below and save again.")
    return render(request, "market/branding.html", {"form": form, "active": "branding"})


@staff_member_required
def media(request):
    form = MediaForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Uploaded.")
        return redirect("manage_media")
    return render(request, "market/media.html", {"form": form, "assets": MediaAsset.objects.all(), "active": "media"})


@staff_member_required
@require_POST
def media_action(request, pk, action):
    asset = get_object_or_404(MediaAsset, pk=pk)
    if action == "toggle":
        asset.is_active = not asset.is_active
        asset.save(update_fields=["is_active"])
        messages.success(request, f"“{asset.title}” is now {'showing' if asset.is_active else 'hidden'}.")
    elif action == "delete":
        asset.delete()
        messages.success(request, "Deleted.")
    return redirect("manage_media")


@staff_member_required
def campaign_list(request):
    items = Campaign.objects.annotate(
        n_entries=Count("entries", filter=Q(entries__status__in=campaigns.OPEN_STATUSES)),
        n_pending=Count("entries", filter=Q(entries__status=CampaignEntry.Status.PENDING)),
    )
    return render(request, "market/campaign_list.html", {"items": items, "active": "campaigns"})


@staff_member_required
def campaign_edit(request, pk=None):
    campaign = get_object_or_404(Campaign, pk=pk) if pk else None
    form = CampaignForm(request.POST or None, request.FILES or None, instance=campaign)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        campaigns.reapply(saved)   # new discount or dates take effect on entries straight away
        messages.success(request, "Campaign saved." if pk else "Campaign created. Publish it when you're ready for sellers to see it.")
        return redirect("campaign_edit", pk=saved.pk)
    entries = campaign.entries.select_related("seller", "listing") if campaign else []
    return render(request, "market/campaign_form.html", {
        "form": form, "campaign": campaign, "entries": entries, "active": "campaigns",
    })


@staff_member_required
@require_POST
def campaign_delete(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    for entry in campaign.entries.filter(applied=True):
        entry.restore()          # give sellers their normal prices back before the campaign disappears
    campaign.delete()
    messages.success(request, "Campaign deleted and sellers' normal prices restored.")
    return redirect("manage_campaigns")


@staff_member_required
@require_POST
def entry_decision(request, pk, action):
    entry = get_object_or_404(CampaignEntry.objects.select_related("campaign"), pk=pk)
    if action == "approve":
        entry.status = CampaignEntry.Status.APPROVED
        entry.save(update_fields=["status"])
        entry.apply()
        messages.success(request, "Entry approved.")
    elif action == "reject":
        entry.restore()
        entry.status = CampaignEntry.Status.REJECTED
        entry.save(update_fields=["status"])
        messages.info(request, "Entry rejected.")
    audit.log(request, f"campaign.entry.{action}", entry.pk, entry.campaign.name)
    return redirect("campaign_edit", pk=entry.campaign_id)
