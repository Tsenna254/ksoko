from django.core.management.base import BaseCommand

from market.campaigns import sync


class Command(BaseCommand):
    help = "Start and end campaign discounts. The site also does this itself once a minute while it has visitors."

    def handle(self, *args, **options):
        sync()
        self.stdout.write("Campaigns synced.")
