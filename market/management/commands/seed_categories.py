from django.core.management.base import BaseCommand

from market.models import Category

NAMES = ["Electronics", "Phones", "Fashion", "Home & Garden", "Motors", "Sports", "Collectibles"]


class Command(BaseCommand):
    help = "Create the starter categories."

    def handle(self, *args, **options):
        for name in NAMES:
            _, created = Category.objects.get_or_create(name=name, parent=None)
            self.stdout.write(f"{'Created' if created else 'Exists'}: {name}")
