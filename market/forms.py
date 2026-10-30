import os
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth.forms import PasswordResetForm, UserCreationForm
from django.template import loader
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
import re

from django.core.files.storage import default_storage
from PIL import Image

from . import emailer
from .models import Campaign, Listing, ListingFee, ListingImage, MediaAsset, Payment, Review, SiteSettings

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
DURATIONS = [("1", "1 day"), ("3", "3 days"), ("5", "5 days"), ("7", "7 days"), ("10", "10 days")]


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A file field that accepts several files in one upload."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        clean_one = super().clean
        if isinstance(data, (list, tuple)):
            return [clean_one(item, initial) for item in data]
        return [clean_one(data, initial)] if data else []


class ListingForm(forms.ModelForm):
    images = MultipleFileField(required=False, label="Photos")
    delete_images = forms.ModelMultipleChoiceField(queryset=ListingImage.objects.none(), required=False)
    duration = forms.ChoiceField(choices=DURATIONS, initial="7", required=False, label="Auction length")
    free_delivery = forms.BooleanField(required=False, initial=True, label="Offer free delivery")

    class Meta:
        model = Listing
        fields = ["title", "category", "condition", "listing_type", "price",
                  "original_price", "quantity", "allow_cod", "delivery_fee", "description"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Brand, model, size or key feature", "autocomplete": "off"}),
            "description": forms.Textarea(attrs={
                "rows": 6, "maxlength": 4000,
                "placeholder": "Describe the item, any flaws, what's included, and why you're selling.",
            }),
            "price": forms.NumberInput(attrs={"min": 1, "step": "1", "inputmode": "numeric", "placeholder": "0"}),
            "original_price": forms.NumberInput(attrs={"min": 1, "step": "1", "inputmode": "numeric", "placeholder": "0"}),
            "quantity": forms.NumberInput(attrs={"min": 1, "step": "1", "inputmode": "numeric"}),
            "delivery_fee": forms.NumberInput(attrs={"min": 0, "step": "1", "inputmode": "numeric", "placeholder": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        f = self.fields
        f["category"].empty_label = "Choose a category"
        f["delivery_fee"].required = False
        pct = f"{settings.COD_LISTING_FEE_PERCENT:f}"
        pct = pct.rstrip("0").rstrip(".") if "." in pct else pct
        f["allow_cod"].help_text = (f"Buyers can pay cash when the item arrives. You pay a one-time fee of {pct}% of the item's "
                                    "value (price x quantity) when you list it. Pay on Delivery switches on once the fee is confirmed.")
        f["images"].widget.attrs["accept"] = "image/jpeg,image/png,image/webp"
        if self.instance.pk:
            f["delete_images"].queryset = self.instance.images.all()
            f["listing_type"].disabled = True      # the format can't change after listing
            del f["duration"]                      # the end time is fixed once created
            f["free_delivery"].initial = self.instance.delivery_fee == 0
            if ListingFee.objects.filter(listing=self.instance, payment__status__in=["awaiting", "paid"]).exists():
                for name in ("price", "quantity", "allow_cod"):      # a fee payment is in progress: don't change what it covers
                    f[name].disabled = True
            in_campaign = self.instance.campaign_entries.filter(
                status__in=["pending", "approved"], campaign__ends_at__gt=timezone.now()).exists()
            if self.instance.bids.exists() or in_campaign:
                f["price"].disabled = True         # locked once people have bid or a campaign discount is on
                f["original_price"].disabled = True
        else:
            del f["delete_images"]

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if len(title) < 5:
            raise forms.ValidationError("Enter a title of at least 5 characters.")
        return title

    def clean_description(self):
        text = self.cleaned_data["description"].strip()
        if len(text) < 20:
            raise forms.ValidationError("Describe your item in at least 20 characters.")
        return text

    def clean_images(self):
        valid = []
        limit = settings.MAX_IMAGE_MB * 1024 * 1024
        for f in self.cleaned_data.get("images") or []:
            if f.size > limit:
                raise forms.ValidationError(f"{f.name} is larger than {settings.MAX_IMAGE_MB} MB.")
            checked = forms.ImageField().clean(f)  # Pillow confirms it is a real image
            if checked.image.format not in ALLOWED_FORMATS:
                raise forms.ValidationError(f"{f.name} must be a JPG, PNG or WebP image.")
            valid.append(checked)
        return valid

    def clean(self):
        data = super().clean()
        ltype, price, orig = (data.get(k) for k in ("listing_type", "price", "original_price"))

        if ltype == Listing.Type.AUCTION:
            data["quantity"] = 1  # an auction sells a single item
            data["allow_cod"] = False
            if orig:
                self.add_error("original_price", "Auctions can't show an original price.")
            if not self.instance.pk and not data.get("duration"):
                self.add_error("duration", "Choose how long the auction runs.")
        if orig and price and orig <= price:
            self.add_error("original_price", "The original price must be higher than the price.")
        elif orig and price and orig > price * 3:
            self.add_error("original_price", "The original price looks too high. It can be at most 3 times the price.")

        if data.get("free_delivery"):
            data["delivery_fee"] = Decimal("0")
        else:
            fee = data.get("delivery_fee")
            if fee is None or fee <= 0:
                self.add_error("delivery_fee", "Enter a delivery fee, or choose free delivery.")

        if "images" not in self.errors:
            existing = self.instance.images.count() if self.instance.pk else 0
            total = existing - len(data.get("delete_images") or []) + len(data.get("images") or [])
            if total < 1:
                self.add_error("images", "Add at least one photo.")
            elif total > settings.MAX_LISTING_IMAGES:
                self.add_error("images", f"A listing can have {settings.MAX_LISTING_IMAGES} photos at most.")
        return data

    @transaction.atomic
    def save(self, seller=None):
        listing = super().save(commit=False)
        creating = listing.pk is None
        if seller is not None:
            listing.seller = seller
        if creating:
            if listing.listing_type == Listing.Type.AUCTION:
                listing.ends_at = timezone.now() + timedelta(days=int(self.cleaned_data["duration"]))
            else:
                listing.ends_at = None
        listing.save()
        for image in self.cleaned_data.get("delete_images") or []:
            image.delete()
        for f in self.cleaned_data.get("images") or []:
            ListingImage.objects.create(listing=listing, image=f)
        return listing


class BidForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=1, label="Your bid (KES)")


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("That email address is already registered. Try signing in instead.")
        return email

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ["product_rating", "seller_rating", "comment"]
        widgets = {"comment": forms.Textarea(attrs={"rows": 4, "maxlength": 1000,
                                                    "placeholder": "Optional: tell other buyers about the item and the seller."})}


def normalize_phone(raw):
    """0712 345 678, +254712345678 and 254712345678 all become 254712345678."""
    digits = re.sub(r"[\s\-+()]", "", raw)
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "71":
        digits = "254" + digits
    if not re.fullmatch(r"254[71]\d{8}", digits):
        raise forms.ValidationError("Enter a valid Kenyan number, for example 0712 345 678.")
    return digits


class CheckoutForm(forms.Form):
    method = forms.ChoiceField(choices=Payment.Method.choices, initial=Payment.Method.MPESA, widget=forms.RadioSelect)
    phone = forms.CharField(required=False, label="M-Pesa phone number")
    delivery_phone = forms.CharField(label="Delivery phone number")
    delivery_address = forms.CharField(
        label="Delivery address", min_length=10, max_length=300, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Building, street and area, with any directions the courier needs.",
    )

    def __init__(self, *args, cod_reason="", **kwargs):
        super().__init__(*args, **kwargs)
        self.cod_reason = cod_reason      # why Pay on Delivery can't be used for this order, if it can't

    def clean(self):
        data = super().clean()
        method = data.get("method")
        if method == Payment.Method.MPESA:
            try:
                data["phone"] = normalize_phone(data.get("phone", ""))
            except forms.ValidationError as exc:
                self.add_error("phone", exc)
        else:
            data["phone"] = ""
        if method == Payment.Method.COD and self.cod_reason:
            self.add_error("method", self.cod_reason)
        if data.get("delivery_phone"):
            try:
                data["delivery_phone"] = normalize_phone(data["delivery_phone"])
            except forms.ValidationError as exc:
                self.add_error("delivery_phone", exc)
        return data


class FeePaymentForm(forms.Form):
    """How a seller pays the Pay-on-Delivery listing fee (online only: a fee can't be paid on delivery)."""
    method = forms.ChoiceField(choices=[(Payment.Method.MPESA, "M-Pesa"), (Payment.Method.CARD, "Card")], initial=Payment.Method.MPESA, widget=forms.RadioSelect)
    phone = forms.CharField(required=False, label="M-Pesa phone number")

    def clean(self):
        data = super().clean()
        if data.get("method") == Payment.Method.MPESA:
            try:
                data["phone"] = normalize_phone(data.get("phone", ""))
            except forms.ValidationError as exc:
                self.add_error("phone", exc)
        else:
            data["phone"] = ""
        return data


DT_FORMAT = "%Y-%m-%dT%H:%M"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".webm"}


def _check_image(f, max_mb, label):
    """Size limit plus a Pillow check that the upload really is a JPG, PNG or WebP picture."""
    if f.size > max_mb * 1024 * 1024:
        raise forms.ValidationError(f"{label} must be smaller than {max_mb} MB.")
    try:
        img = Image.open(f)
        img.verify()
        ok = img.format in ALLOWED_FORMATS
    except Exception:
        ok = False
    f.seek(0)
    if not ok:
        raise forms.ValidationError(f"{label} must be a JPG, PNG or WebP image.")


class SiteSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ["site_name", "logo", "hero_headline", "hero_subtext", "hero_background", "hero_overlay", "site_background", "background_overlay"]
        labels = {"logo": "Website logo", "hero_background": "Homepage banner background", "site_background": "Page background image"}
        widgets = {
            "background_overlay": forms.NumberInput(attrs={"min": 0, "max": 100}),
            "hero_overlay": forms.NumberInput(attrs={"min": 0, "max": 100}),
        }

    IMAGE_FIELDS = ("logo", "hero_background", "site_background")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._old = {n: getattr(self.instance, n).name for n in self.IMAGE_FIELDS}
        for name in self.IMAGE_FIELDS:
            self.fields[name].widget.attrs["accept"] = "image/jpeg,image/png,image/webp"

    def clean(self):
        data = super().clean()
        for name in self.IMAGE_FIELDS:
            f = data.get(name)
            if f and hasattr(f, "size") and name in self.files:   # only newly uploaded files
                try:
                    _check_image(f, settings.MAX_PROMO_IMAGE_MB, self.fields[name].label)
                except forms.ValidationError as exc:
                    self.add_error(name, exc)
        return data

    def save(self, commit=True):
        site = super().save(commit)
        for name, old in self._old.items():           # tidy up files that were replaced or removed
            if old and old != getattr(site, name).name:
                default_storage.delete(old)
        return site


class MediaForm(forms.ModelForm):
    class Meta:
        model = MediaAsset
        fields = ["title", "file", "placement", "link_url", "sort_order", "is_active"]
        widgets = {"link_url": forms.TextInput(attrs={"placeholder": "/campaigns/black-friday/ or https://..."})}

    def clean_file(self):
        f = self.cleaned_data["file"]
        ext = os.path.splitext(f.name)[1].lower()
        if ext in IMAGE_EXTS:
            _check_image(f, settings.MAX_PROMO_IMAGE_MB, "Images")
            self.kind = MediaAsset.Kind.IMAGE
        elif ext in VIDEO_EXTS:
            if f.size > settings.MAX_PROMO_VIDEO_MB * 1024 * 1024:
                raise forms.ValidationError(f"Videos must be smaller than {settings.MAX_PROMO_VIDEO_MB} MB.")
            head = f.read(12)
            f.seek(0)
            is_mp4 = ext == ".mp4" and head[4:8] == b"ftyp"
            is_webm = ext == ".webm" and head[:4] == b"\x1a\x45\xdf\xa3"
            if not (is_mp4 or is_webm):                     # the file's contents must match its extension
                raise forms.ValidationError("That doesn't look like a real MP4 or WebM video.")
            self.kind = MediaAsset.Kind.VIDEO
        else:
            raise forms.ValidationError("Upload a JPG, PNG or WebP image, or an MP4 or WebM video.")
        return f

    def clean_link_url(self):
        url = self.cleaned_data["link_url"].strip()
        if url and (re.search(r"[\\\s]", url) or not (re.match(r"^/(?![/\\])", url) or re.match(r"^https?://[^/\s]+", url))):
            raise forms.ValidationError("Use a page on this site such as /listings/, or a full http(s) address.")
        return url

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.kind = self.kind
        if commit:
            obj.save()
        return obj


class CampaignForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ["name", "description", "banner", "badge_text", "discount_percent", "categories",
                  "max_listings_per_seller", "min_seller_rating", "requires_approval",
                  "starts_at", "ends_at", "join_deadline", "is_published", "featured"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "categories": forms.CheckboxSelectMultiple,
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DT_FORMAT),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DT_FORMAT),
            "join_deadline": forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DT_FORMAT),
            "min_seller_rating": forms.NumberInput(attrs={"step": "0.1", "min": 0, "max": 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("starts_at", "ends_at", "join_deadline"):
            self.fields[name].input_formats = [DT_FORMAT]
        self.fields["banner"].widget.attrs["accept"] = "image/jpeg,image/png,image/webp"

    def clean(self):
        data = super().clean()
        starts, ends, deadline = data.get("starts_at"), data.get("ends_at"), data.get("join_deadline")
        if starts and ends and ends <= starts:
            self.add_error("ends_at", "The campaign must end after it starts.")
        if deadline and ends and deadline > ends:
            self.add_error("join_deadline", "The join deadline can't be after the campaign ends.")
        banner = data.get("banner")
        if banner and hasattr(banner, "size") and "banner" in self.files:
            try:
                _check_image(banner, settings.MAX_PROMO_IMAGE_MB, "The banner")
            except forms.ValidationError as exc:
                self.add_error("banner", exc)
        return data


class CampaignJoinForm(forms.Form):
    listings = forms.ModelMultipleChoiceField(queryset=Listing.objects.none(), widget=forms.CheckboxSelectMultiple)

    def __init__(self, queryset, slots, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["listings"].queryset = queryset
        self.slots = slots

    def clean_listings(self):
        chosen = self.cleaned_data["listings"]
        if not chosen:
            raise forms.ValidationError("Choose at least one listing.")
        if len(chosen) > self.slots:
            raise forms.ValidationError(f"You can add {self.slots} more listing(s) to this campaign.")
        return chosen


class QueuedPasswordResetForm(PasswordResetForm):
    """Django's reset form, but the email is queued (sent in the background, retried on failure)."""

    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email, html_email_template_name=None):
        subject = "".join(loader.render_to_string(subject_template_name, context).splitlines())
        text = loader.render_to_string(email_template_name, context)
        html = loader.render_to_string(html_email_template_name, context) if html_email_template_name else ""
        emailer.queue_email(
            kind="password_reset", to_email=to_email, subject=subject, text=text, html=html, user=context.get("user"),
            expires_at=timezone.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT),
        )
