from django.contrib import admin

from . import audit

from .models import (Bid, Campaign, CampaignEntry, CartItem, Category, Listing, ListingImage, MediaAsset, Order,
                     PageView, Payment, Review, SearchEvent, SiteSettings, Watch)


class ListingImageInline(admin.TabularInline):
    model = ListingImage
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ("title", "seller", "category", "listing_type", "price", "status", "created_at")
    list_filter = ("status", "listing_type", "category")
    search_fields = ("title", "seller__username")
    inlines = [ListingImageInline]


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
