from django.core.management.base import BaseCommand

from market.models import PageView
from market.security import privacy_hash


class Command(BaseCommand):
    help = "One-off: replace real session keys stored by older versions in PageView with keyed hashes."

    def handle(self, *args, **options):
        fixed = 0
        for view in PageView.objects.exclude(session_key="").iterator():
            if len(view.session_key) == 32:          # a raw Django session key (our hashes are 40 characters)
                PageView.objects.filter(pk=view.pk).update(session_key=privacy_hash("sess", view.session_key))
                fixed += 1
        self.stdout.write(f"Scrubbed {fixed} stored session key(s).")
