from django import template
from django.templatetags.static import static

register = template.Library()


@register.filter
def stars(value):
    """4.6 -> '★★★★★' (rounded to the nearest whole star), None -> ''."""
    try:
        full = max(0, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return ""
    return "★" * full + "☆" * (5 - full)


# Built-in artwork for categories that have no uploaded picture. The first keyword found in the name wins,
# so the specific ones (phones, laptops...) come before the broad ones (electronics, home...).
_ART = [
    (("audio", "headphone", "speaker", "earbud"), "img/tile-audio.png"),   # before "phone", which "headphones" contains
    (("phone", "mobile"), "img/tile-phones.png"), (("laptop", "computer"), "img/tile-laptops.png"),
    (("television", " tv", "tv "), "img/tile-tv.png"),
    (("camera", "photograph"), "img/tile-cameras.png"), (("gaming", "console"), "img/tile-gaming.png"),
    (("electronic", "tech"), "img/cat-electronics.png"), (("fashion", "cloth", "apparel", "shoe", "wear"), "img/cat-fashion.png"),
    (("home", "furniture", "living", "garden", "kitchen"), "img/cat-home.png"),
    (("beauty", "personal care", "cosmetic"), "img/cat-beauty.png"),
    (("sport", "outdoor", "fitness"), "img/cat-sports.png"), (("toy", "kid", "baby"), "img/cat-toys.png"),
]


@register.filter
def cat_image(category):
    """The picture for a category: the one uploaded in the admin, else built-in artwork by name, else ''."""
    if getattr(category, "image", None):
        return category.image.url
    name = f" {category.name.lower()} "
    for words, path in _ART:
        if any(w in name for w in words):
            return static(path)
    return ""
