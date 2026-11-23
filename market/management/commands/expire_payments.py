from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from market.models import Payment


class Command(BaseCommand):
    help = "Cancel payments that never received any money, and return their stock. Run from cron."

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=60)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=options["minutes"])
        count = 0
        for payment in Payment.objects.filter(status=Payment.Status.AWAITING, created_at__lt=cutoff):
            if payment.release(Payment.Status.CANCELLED, "Expired: no payment received"):
                count += 1
        self.stdout.write(f"Expired {count} unpaid payment(s).")
