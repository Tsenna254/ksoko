from django.contrib import admin
from django.db.models import Count, F
from django.db.models.functions import Coalesce

from . import audit
from .models import FilterOption, FilterParameter, ListingAttribute

from .models import (Bid, Campaign, CampaignEntry, CartItem, Category, Listing, ListingImage, MediaAsset, Order,
                     PageView, Payment, Review, SearchEvent, SiteSettings, Watch)


class ListingAttributeInline(admin.TabularInline):
    model = ListingAttribute
    extra = 0
    fields = ("parameter", "option", "number", "yes")


class ListingImageInline(admin.TabularInline):
    model = ListingImage
    extra = 0


class SubcategoryInline(admin.TabularInline):
    """Add sub categories right on a main category's page."""
    model = Category
    fk_name = "parent"
    fields = ("name",)
    extra = 3
    verbose_name = "sub category"
    verbose_name_plural = "sub categories"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name_in_tree", "parent", "slug", "listing_count")
    list_filter = (("parent", admin.RelatedOnlyFieldListFilter),)
    search_fields = ("name", "parent__name")
    fields = ("name", "parent", "slug")
    inlines = [SubcategoryInline]
    # The admin applies its own ordering over the queryset's, so say it here: each main category, then its sub categories.
    ordering = (Coalesce("parent__name", "name"), F("parent").asc(nulls_first=True), "name")

    def get_queryset(self, request):
        return Category.objects.select_related("parent").annotate(n_listings=Count("listings"))

    def get_inline_instances(self, request, obj=None):
        if obj is not None and obj.parent_id:      # a sub category can't have sub categories of its own
            return []
        return super().get_inline_instances(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "parent":              # only main categories can be a parent
            kwargs["queryset"] = Category.objects.top()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description="Category", ordering="name")
    def name_in_tree(self, obj):
        return f"\u2003\u21b3 {obj.name}" if obj.parent_id else obj.name

    @admin.display(description="Listings")
    def listing_count(self, obj):
        return obj.n_listings


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ("title", "seller", "category", "listing_type", "price", "status", "created_at")
    list_filter = ("status", "listing_type", "category")
    search_fields = ("title", "seller__username")
    inlines = [ListingImageInline, ListingAttributeInline]


admin.site.register(Bid)
admin.site.register(Watch)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "buyer", "seller", "status", "total", "payment", "created_at")
    list_filter = ("status", "source", "created_at")
    search_fields = ("title", "buyer__username", "seller__username")


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("listing", "reviewer", "seller", "product_rating", "seller_rating", "created_at")
    list_filter = ("product_rating", "seller_rating")


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ("path", "user", "listing", "created_at")
    date_hierarchy = "created_at"


@admin.action(description="Confirm selected payments (complete the orders)")
def confirm_payments(modeladmin, request, queryset):
    done = 0
    for p in queryset:
        if p.confirm(request.user):      # only payments the customer has paid
            audit.log(request, "payment.confirm", p.reference, "via Django admin")
            done += 1
    modeladmin.message_user(request, f"{done} payment(s) confirmed.")


@admin.action(description="Reject selected payments (return stock)")
def reject_payments(modeladmin, request, queryset):
    done = sum(1 for p in queryset if p.release(Payment.Status.REJECTED, f"Rejected by {request.user.username}"))
    modeladmin.message_user(request, f"{done} payment(s) rejected.")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "user", "purpose", "method", "amount", "status", "provider_ref", "created_at")
    list_filter = ("status", "method", "purpose")
    search_fields = ("reference", "provider_ref", "user__username", "phone")
    readonly_fields = ("reference", "created_at", "paid_at", "confirmed_at", "confirmed_by")
    actions = [confirm_payments, reject_payments]


admin.site.register(CartItem)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()   # there is only ever one set of site settings

    def has_delete_permission(self, request, obj=None):
        return False
admin.site.register(MediaAsset)
admin.site.register(CampaignEntry)


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "discount_percent", "starts_at", "ends_at", "is_published")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SearchEvent)
class SearchEventAdmin(admin.ModelAdmin):
    list_display = ("query", "user", "results", "created_at")
    search_fields = ("query", "user__username")
    date_hierarchy = "created_at"


from .models import AccountSecurity, AuditLog  # noqa: E402

admin.site.register(AccountSecurity)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """A read-only record of sensitive staff actions."""
    list_display = ("created_at", "actor", "action", "target", "detail")
    list_filter = ("action",)
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from .models import OutboundEmail  # noqa: E402


@admin.register(OutboundEmail)
class OutboundEmailAdmin(admin.ModelAdmin):
    """Delivery records. Message text is never shown here (it holds codes and links, and is wiped after sending)."""
    list_display = ("created_at", "kind", "to_email", "status", "attempts", "sent_at", "last_error")
    list_filter = ("status", "kind")
    search_fields = ("to_email",)
    exclude = ("body_text", "body_html")
    readonly_fields = [f.name for f in OutboundEmail._meta.fields if f.name not in ("body_text", "body_html")]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


from .models import ListingFee  # noqa: E402


@admin.register(ListingFee)
class ListingFeeAdmin(admin.ModelAdmin):
    list_display = ("listing", "seller", "covers_value", "percent", "amount", "status", "paid_at")
    list_filter = ("status",)
    readonly_fields = [f.name for f in ListingFee._meta.fields]

    def has_add_permission(self, request):
        return False


from django import forms as djforms  # noqa: E402


class FilterOptionInline(admin.TabularInline):
    model = FilterOption
    extra = 3
    fields = ("value", "sort_order")


class FilterParameterAdminForm(djforms.ModelForm):
    quick_options = djforms.CharField(
        label="Quick add options", required=False,
        widget=djforms.Textarea(attrs={"rows": 5, "placeholder": "Samsung\nApple\nTecno\nInfinix"}),
        help_text="For 'Pick one from a list'. Type one option per line to add many at once. They're added when you save.",
    )

    class Meta:
        model = FilterParameter
        fields = "__all__"


@admin.register(FilterParameter)
class FilterParameterAdmin(admin.ModelAdmin):
    """Create the filters shoppers use and sellers fill in: Brand, Colour, Storage, Dual SIM..."""
    form = FilterParameterAdminForm
    list_display = ("name", "kind", "unit", "where", "required", "show_in_filter", "is_active", "sort_order")
    list_editable = ("is_active", "show_in_filter", "sort_order")
    list_filter = ("kind", "is_active")
    search_fields = ("name",)
    filter_horizontal = ("categories",)
    inlines = [FilterOptionInline]
    fieldsets = (
        (None, {"fields": ("name", "kind", "unit", "quick_options")}),
        ("Where it applies", {"fields": ("categories",)}),
        ("Behaviour", {"fields": ("required", "show_in_filter", "is_active", "sort_order")}),
    )

    @admin.display(description="Applies to")
    def where(self, obj):
        names = [c.full_name for c in obj.categories.all()]
        return ", ".join(names) if names else "Every category"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("categories__parent")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        lines = [ln.strip()[:60] for ln in form.cleaned_data.get("quick_options", "").splitlines() if ln.strip()]
        if obj.kind == FilterParameter.Kind.CHOICE and lines:
            have = {o.value.lower() for o in obj.options.all()}
            order = obj.options.count()
            for value in dict.fromkeys(lines):                       # keeps the typed order, drops repeats
                if value.lower() not in have:
                    order += 1
                    FilterOption.objects.create(parameter=obj, value=value, sort_order=order)
                    have.add(value.lower())
