from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from market.models import PageView, SearchEvent


class Command(BaseCommand):
    help = "Delete search history older than the retention period (default SEARCH_HISTORY_DAYS) and unlink old page views."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.SEARCH_HISTORY_DAYS)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        searches, _ = SearchEvent.objects.filter(created_at__lt=cutoff).delete()
        views = PageView.objects.filter(created_at__lt=cutoff).exclude(visitor_id="", user=None).update(user=None, visitor_id="")
        self.stdout.write(f"Deleted {searches} old search(es); unlinked {views} old page view(s) from people.")
