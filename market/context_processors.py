from .models import Category, SiteSettings


def categories(request):
    return {"nav_categories": Category.objects.all()}


def cart(request):
    """Feeds the cart menu in the page header."""
    if not request.user.is_authenticated:
        return {"cart_count": 0}
    from .cart import cart_summary
    summary = cart_summary(request.user)
    return {"cart_count": summary["count"], "mini_cart": summary}


def site(request):
    """Logo, background images and hero text for every page."""
    return {"site": SiteSettings.load()}
