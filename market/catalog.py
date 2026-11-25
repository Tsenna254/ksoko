"""The category list as JSON, for the two-step category picker on the sell page (see category_picker.html)."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .models import Category


@require_GET
def category_tree(request):
    """Main categories, each with its sub categories, in alphabetical order."""
    mains = Category.objects.top().order_by("name").prefetch_related("children")
    data = [
        {
            "id": main.pk,
            "name": main.name,
            "children": [{"id": sub.pk, "name": sub.name}
                         for sub in sorted(main.children.all(), key=lambda s: s.name.lower())],
        }
        for main in mains
    ]
    return JsonResponse(data, safe=False)
