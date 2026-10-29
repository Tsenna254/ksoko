from django import template

register = template.Library()


@register.filter
def stars(value):
    """4.6 -> '★★★★★' (rounded to the nearest whole star), None -> ''."""
    try:
        full = max(0, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return ""
    return "★" * full + "☆" * (5 - full)
