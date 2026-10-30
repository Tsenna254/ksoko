import os
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Avg, Count, OuterRef, Q, Subquery
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .security import safe_ext

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXTS = {".mp4", ".webm"}


class Category(models.Model):
    name = models.CharField(max_length=60, unique=True)
    slug = models.SlugField(max_length=70, unique=True, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ListingQuerySet(models.QuerySet):
    def active(self):
        """Live listings: buy-it-now items, or auctions that have not ended."""
        return self.filter(status="active").filter(
            Q(listing_type="buy") | Q(ends_at__gt=timezone.now())
        )

    def with_counts(self):
        """Adds bid counts plus product and vendor rating figures to each listing."""
        seller_reviews = Review.objects.filter(seller=OuterRef("seller")).order_by().values("seller")
        return self.annotate(
            num_bids=Count("bids", distinct=True),
            num_reviews=Count("reviews", distinct=True),
            product_avg=Avg("reviews__product_rating"),
            seller_avg=Subquery(seller_reviews.annotate(a=Avg("seller_rating")).values("a")),
            seller_n=Subquery(seller_reviews.annotate(n=Count("id")).values("n")),
        )


class Listing(models.Model):
    class Type(models.TextChoices):
        BUY = "buy", "Buy it now"
        AUCTION = "auction", "Auction"

    class Condition(models.TextChoices):
        NEW = "new", "Brand new"
        REFURBISHED = "refurbished", "Refurbished"
        USED = "used", "Pre-owned"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SOLD = "sold", "Sold"
        ENDED = "ended", "Ended"

    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="listings")
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="purchases"
    )
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="listings")
    title = models.CharField(max_length=120)
    description = models.TextField(max_length=4000)
    condition = models.CharField(max_length=20, choices=Condition.choices, default=Condition.NEW)
    listing_type = models.CharField(max_length=10, choices=Type.choices, default=Type.BUY)
    # For auctions this is the starting price and then the current highest bid.
    price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("1"))])
    original_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("1"))]
    )
    delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(Decimal("0"))],
        help_text="Enter 0 for free delivery.",
    )
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)], help_text="How many are available.")
    allow_cod = models.BooleanField("Offer Pay on Delivery", default=False)
    ends_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ListingQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "listing_type", "-created_at"])]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("listing_detail", args=[self.pk])

    @property
    def is_auction(self):
        return self.listing_type == self.Type.AUCTION

    @property
    def is_open(self):
        if self.status != self.Status.ACTIVE:
            return False
        return not (self.is_auction and self.ends_at and self.ends_at <= timezone.now())

    @property
    def cover(self):
        images = list(self.images.all())  # uses prefetch_related cache when present
        return images[0] if images else None

    @property
    def discount_percent(self):
        if self.original_price and self.original_price > self.price:
            return int((1 - self.price / self.original_price) * 100)
        return 0

    @property
    def cod_covered(self):
        """Value of this item already covered by paid Pay-on-Delivery listing fees."""
        return sum((f.covers_value for f in self.fees.all() if f.status == "paid"), Decimal("0"))

    @property
    def cod_active(self):
        """Pay on Delivery is offered: the seller opted in and their one-time fee covers the item's current value."""
        return bool(self.allow_cod and self.listing_type == self.Type.BUY and self.quantity > 0
                    and self.cod_covered >= self.price * self.quantity)

    @property
    def open_fee(self):
        return next((f for f in self.fees.all() if f.status == "unpaid"), None)

    @property
    def active_campaign(self):
        """The running campaign this listing is discounted by, if any (uses prefetched entries)."""
        for entry in self.campaign_entries.all():
            if entry.applied:
                return entry.campaign
        return None

    @property
    def min_next_bid(self):
        if not self.bids.exists():
            return self.price
        step = max(Decimal("50"), (self.price * Decimal("0.02") / 50).to_integral_value() * 50)
        return self.price + step


def listing_image_path(instance, filename):
    # Random file names stop customers overwriting each other's files and avoid
    # trusting the original name.
    ext = safe_ext(filename, IMAGE_EXTS, ".jpg")   # never trust the uploaded extension
    return f"listings/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class ListingImage(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=listing_image_path)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return os.path.basename(self.image.name)


@receiver(post_delete, sender=ListingImage)
def delete_image_file(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)


class Bid(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="bids")
    bidder = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bids")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    placed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-amount", "-placed_at"]

    def __str__(self):
        return f"{self.bidder} bid {self.amount} on {self.listing_id}"


class Watch(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="watching")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="watchers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "listing"], name="unique_watch")]
        ordering = ["-created_at"]


class Order(models.Model):
    """One completed transaction: a fixed-price purchase or a won auction."""

    class Source(models.TextChoices):
        BUY = "buy", "Buy it now"
        AUCTION = "auction", "Auction win"

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Awaiting payment"
        PENDING = "pending", "Payment pending confirmation"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="orders")
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales")
    title = models.CharField(max_length=120)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.BUY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNPAID, db_index=True)
    payment = models.ForeignKey("Payment", null=True, blank=True, on_delete=models.PROTECT, related_name="orders")
    quantity = models.PositiveIntegerField(default=1)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        self.total = self.amount + self.delivery_fee
        if not self.title:
            self.title = self.listing.title
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.pk}: {self.title}"


RATING = [MinValueValidator(1), MaxValueValidator(5)]


class Review(models.Model):
    """A buyer's rating of the product and of the vendor, one per order."""

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="review")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="reviews")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews_received")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews_written")
    product_rating = models.PositiveSmallIntegerField(validators=RATING)
    seller_rating = models.PositiveSmallIntegerField(validators=RATING)
    comment = models.TextField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class PageView(models.Model):
    """One page view, recorded by TrafficMiddleware for the admin dashboard."""

    path = models.CharField(max_length=300)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    session_key = models.CharField(max_length=40, blank=True, db_index=True)   # a keyed hash, never the real key
    visitor_id = models.CharField(max_length=40, blank=True, db_index=True)
    listing = models.ForeignKey(Listing, null=True, blank=True, on_delete=models.SET_NULL, related_name="views")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class CartItem(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cart_items")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="in_carts")
    quantity = models.PositiveIntegerField(default=1)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]
        constraints = [models.UniqueConstraint(fields=["user", "listing"], name="unique_cart_item")]


class Payment(models.Model):
    """One payment covering one or more orders. Staff confirm it before orders complete."""

    class Method(models.TextChoices):
        MPESA = "mpesa", "M-Pesa"
        CARD = "card", "Card"
        COD = "cod", "Pay on delivery"

    class Status(models.TextChoices):
        AWAITING = "awaiting", "Awaiting payment"
        COD_PENDING = "cod_pending", "Pay on delivery"
        PAID = "paid", "Paid, awaiting confirmation"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    class Purpose(models.TextChoices):
        ORDER = "order", "Order"
        LISTING_FEE = "listing_fee", "Listing fee"

    reference = models.CharField(max_length=20, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=10, choices=Method.choices)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.AWAITING, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    purpose = models.CharField(max_length=12, choices=Purpose.choices, default=Purpose.ORDER)
    delivery_phone = models.CharField(max_length=15, blank=True)
    delivery_address = models.CharField(max_length=300, blank=True)
    phone = models.CharField(max_length=15, blank=True)
    mpesa_checkout_id = models.CharField(max_length=100, blank=True, db_index=True)
    provider_ref = models.CharField("M-Pesa code / card transaction ID", max_length=60, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    paid_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider_ref"], condition=~Q(provider_ref=""), name="unique_provider_ref")
        ]

    def __str__(self):
        return f"{self.reference} ({self.get_method_display()}, KES {self.amount})"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = "KS" + uuid.uuid4().hex[:8].upper()
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status in (self.Status.AWAITING, self.Status.PAID, self.Status.COD_PENDING)

    def mark_paid(self, provider_ref, note=""):
        """The customer has paid (M-Pesa code received or card charge verified). Staff still confirm."""
        if self.status != self.Status.AWAITING:
            return False
        self.status = self.Status.PAID
        self.provider_ref = provider_ref
        self.paid_at = timezone.now()
        if note:
            self.note = note[:255]
        self.save(update_fields=["status", "provider_ref", "paid_at", "note"])
        return True

    @transaction.atomic
    def mark_cod_delivered(self):
        """The seller says the order was delivered and the cash collected. Staff still confirm."""
        if self.status != self.Status.COD_PENDING:
            return False
        self.status = self.Status.PAID
        self.paid_at = timezone.now()
        self.note = "Seller marked delivered and cash collected"
        self.save(update_fields=["status", "paid_at", "note"])
        return True

    @transaction.atomic
    def confirm(self, staff=None, manual_reason=""):
        """Staff verified the money arrived: complete the orders.

        Only payments the customer has reported or made ("paid") can be confirmed. An unpaid payment can be
        confirmed by hand only with a written reason, which is kept on the payment.
        """
        if self.status == self.Status.PAID:
            pass
        elif self.status in (self.Status.AWAITING, self.Status.COD_PENDING) and manual_reason.strip():
            self.note = f"Confirmed by hand: {manual_reason.strip()}"[:255]
        else:
            return False
        now = timezone.now()
        self.status = self.Status.CONFIRMED
        self.confirmed_at, self.confirmed_by = now, staff
        self.save(update_fields=["status", "confirmed_at", "confirmed_by", "note"])
        self.orders.update(status=Order.Status.COMPLETED, completed_at=now)
        self.listing_fees.update(status="paid", paid_at=now)     # a listing-fee payment switches Pay on Delivery on
        return True

    @transaction.atomic
    def release(self, status, note=""):
        """End an unsuccessful payment: restock cancelled purchases, re-open won auctions for payment."""
        if not self.is_open:
            return False
        for order in list(self.orders.all()):
            if order.source == Order.Source.AUCTION:
                order.status, order.payment = Order.Status.UNPAID, None
                order.save(update_fields=["status", "payment"])
            else:
                order.status = Order.Status.CANCELLED
                order.save(update_fields=["status"])
                listing = Listing.objects.select_for_update().get(pk=order.listing_id)
                listing.quantity += order.quantity
                if listing.status == Listing.Status.SOLD:
                    listing.status = Listing.Status.ACTIVE
                listing.save(update_fields=["quantity", "status", "updated_at"])
        self.listing_fees.update(payment=None)                    # an unpaid listing fee can be paid again later
        self.status = status
        if note:
            self.note = note[:255]
        self.save(update_fields=["status", "note"])
        return True


# ---- Site branding, promotional media and campaigns --------------------------

def site_image_path(instance, filename):
    return f"site/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{safe_ext(filename, IMAGE_EXTS, '.jpg')}"


def promo_media_path(instance, filename):
    return f"promo/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{safe_ext(filename, IMAGE_EXTS | MEDIA_EXTS, '.bin')}"


class SiteSettings(models.Model):
    """One row holding the site's logo, background images and hero text. Edited from the admin menu."""

    site_name = models.CharField(max_length=60, default="Ksoko")
    logo = models.ImageField(upload_to=site_image_path, blank=True)
    hero_background = models.ImageField(upload_to=site_image_path, blank=True)
    site_background = models.ImageField(upload_to=site_image_path, blank=True)
    background_overlay = models.PositiveSmallIntegerField(
        default=65, validators=[MaxValueValidator(100)],
        help_text="How strongly the page colour covers the background image, from 0 (image only) to 100 (hidden). Try 50 to 70.",
    )
    hero_overlay = models.PositiveSmallIntegerField(
        default=55, validators=[MaxValueValidator(100)],
        help_text="Green tint over the banner photo, from 0 (photo only) to 100 (solid green). Lower it to show more of your photo.",
    )
    hero_headline = models.CharField(max_length=80, default="Your market, open all day.")
    hero_subtext = models.CharField(
        max_length=200,
        default="Buy, sell and bid on phones, fashion, furniture, cars and more from sellers you can rate and trust.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "site settings"

    @classmethod
    def load(cls):
        # Always the oldest row, so the site and the editing forms can never disagree.
        return cls.objects.order_by("pk").first() or cls.objects.create()

    def __str__(self):
        return self.site_name


class MediaAsset(models.Model):
    """An uploaded promotional image or video shown on the site."""

    class Placement(models.TextChoices):
        HOME = "home", "Homepage banner"
        LISTINGS = "listings", "Top of listing pages"

    class Kind(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"

    title = models.CharField(max_length=100)
    file = models.FileField(upload_to=promo_media_path)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.IMAGE)
    placement = models.CharField(max_length=10, choices=Placement.choices, default=Placement.HOME)
    link_url = models.CharField("Link (optional)", max_length=300, blank=True)
    sort_order = models.PositiveIntegerField(default=0, help_text="Lower numbers show first.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "-created_at"]

    def __str__(self):
        return self.title


@receiver(post_delete, sender=MediaAsset)
def delete_media_file(sender, instance, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


class Campaign(models.Model):
    """A promotion run by the store that sellers can opt in to, with a set discount."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=90, unique=True, blank=True)
    description = models.TextField(blank=True, max_length=1000)
    banner = models.ImageField(upload_to=promo_media_path, blank=True)
    badge_text = models.CharField(max_length=30, blank=True, help_text="Short label shown on discounted items, e.g. Black Friday.")
    discount_percent = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(90)],
        help_text="Percentage taken off each joined listing while the campaign runs.",
    )
    categories = models.ManyToManyField(Category, blank=True, related_name="campaigns", help_text="Leave empty to allow every category.")
    max_listings_per_seller = models.PositiveIntegerField(default=3, validators=[MinValueValidator(1)])
    min_seller_rating = models.DecimalField(
        max_digits=2, decimal_places=1, default=0, validators=[MinValueValidator(0), MaxValueValidator(5)],
        help_text="Sellers need at least this average rating to join. 0 means no minimum.",
    )
    requires_approval = models.BooleanField(default=True, help_text="If on, staff approve each entry before the discount starts.")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    join_deadline = models.DateTimeField(null=True, blank=True, help_text="Last moment sellers can join. Empty means until the campaign ends.")
    is_published = models.BooleanField(default=False, help_text="Draft campaigns are hidden from sellers and shoppers.")
    featured = models.BooleanField(default=True, help_text="Show on the homepage.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "campaign"
            slug, n = base, 2
            while Campaign.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("campaign_detail", args=[self.slug])

    @property
    def phase(self):
        now = timezone.now()
        if not self.is_published:
            return "draft"
        if now < self.starts_at:
            return "upcoming"
        return "live" if now < self.ends_at else "ended"

    @property
    def is_joinable(self):
        now = timezone.now()
        return self.is_published and now < self.ends_at and (self.join_deadline is None or now <= self.join_deadline)

    @property
    def badge(self):
        return self.badge_text or self.name

    def discounted(self, price):
        new = (price * Decimal(100 - self.discount_percent) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return max(new, Decimal("1"))


class CampaignEntry(models.Model):
    """A seller's listing taking part in a campaign."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="entries")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="campaign_entries")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="campaign_entries")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    applied = models.BooleanField(default=False, help_text="True while the discount is on the listing.")
    price_before = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    original_before = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["campaign", "listing"], name="unique_campaign_listing")]

    @transaction.atomic
    def apply(self):
        """Put the campaign discount on the listing (only while the campaign is live)."""
        if self.applied or self.status != self.Status.APPROVED or self.campaign.phase != "live":
            return False
        listing = Listing.objects.select_for_update().get(pk=self.listing_id)
        if listing.listing_type != Listing.Type.BUY or not listing.is_open:
            return False
        self.price_before, self.original_before = listing.price, listing.original_price
        listing.original_price = listing.price          # shown as the crossed-out "was" price
        listing.price = self.campaign.discounted(listing.price)
        listing.save(update_fields=["price", "original_price", "updated_at"])
        self.applied = True
        self.save(update_fields=["price_before", "original_before", "applied"])
        return True

    @transaction.atomic
    def restore(self):
        """Take the discount off again and put the seller's own prices back."""
        if not self.applied:
            return False
        listing = Listing.objects.select_for_update().get(pk=self.listing_id)
        listing.price, listing.original_price = self.price_before, self.original_before
        listing.save(update_fields=["price", "original_price", "updated_at"])
        self.applied = False
        self.save(update_fields=["applied"])
        from .cod import sync_cod_fee          # the restored price may need a bigger Pay-on-Delivery fee
        sync_cod_fee(listing)
        return True


class SearchEvent(models.Model):
    """One search a visitor made. Identified by user id, or an anonymous visitor id for guests."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="searches")
    visitor_id = models.CharField(max_length=40, blank=True, db_index=True)
    ip_hash = models.CharField(max_length=40, blank=True, db_index=True)   # salted hash, never the raw IP address
    query = models.CharField(max_length=100)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    results = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self):
        return self.query


# ---- Accounts: email verification, trusted networks, audit trail ---------------------------

class AccountSecurity(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="security")
    email_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} ({'verified' if self.email_verified else 'unverified'})"


class KnownIP(models.Model):
    """Networks (stored as keyed hashes) this account has signed in from after proving it with an emailed code."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="known_ips")
    ip_hash = models.CharField(max_length=40)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "ip_hash"], name="unique_known_ip")]


class VerificationCode(models.Model):
    """A six-digit code emailed to a user. Only a keyed hash of the code is stored."""

    class Purpose(models.TextChoices):
        REGISTER = "register", "Email verification"
        LOGIN = "login", "Sign-in from a new network"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="codes")
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    code_hash = models.CharField(max_length=40)
    ip_hash = models.CharField(max_length=40, blank=True)       # sign-in codes only work from the network that asked
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=200, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    ip_hash = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class OutboundEmail(models.Model):
    """An email waiting to be sent, being sent, or already handled. See market/emailer.py.

    The message text is kept only while the email is waiting (it contains a code or a reset link) and is
    wiped as soon as the email is sent, fails for good, expires or is cancelled.
    """

    class Kind(models.TextChoices):
        REGISTER_CODE = "register_code", "Email verification code"
        LOGIN_CODE = "login_code", "New-network sign-in code"
        PASSWORD_RESET = "password_reset", "Password reset link"

    class Status(models.TextChoices):
        QUEUED = "queued", "Waiting to send"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired before sending"
        CANCELLED = "cancelled", "Replaced by a newer email"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    to_email = models.EmailField()
    subject = models.CharField(max_length=200)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Not sent after this moment (a code is useless once it expires).")

    class Meta:
        ordering = ["-created_at"]


class ListingFee(models.Model):
    """The one-time fee a seller pays (a percentage of the item's value) to offer Pay on Delivery."""

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Paid"

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="fees")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    covers_value = models.DecimalField(max_digits=12, decimal_places=2, help_text="The part of the item's value this fee covers.")
    percent = models.DecimalField(max_digits=5, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="listing_fees")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.UNPAID, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.listing} fee KES {self.amount} ({self.status})"
