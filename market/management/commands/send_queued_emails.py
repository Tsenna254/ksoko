import time

from django.core.management.base import BaseCommand

from market.emailer import process_outbox


class Command(BaseCommand):
    help = ("Send queued verification and reset emails. Run once from cron, or with --loop as a small always-on "
            "worker (e.g. a systemd service). The site also does this by itself, so this is optional.")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="keep running")
        parser.add_argument("--interval", type=int, default=10, help="seconds between checks with --loop")

    def handle(self, *args, **options):
        if not options["loop"]:
            self.stdout.write(f"Sent {process_outbox()} email(s).")
            return
        self.stdout.write("Email worker running. Press Ctrl+C to stop.")
        try:
            while True:
                try:
                    process_outbox()
                except Exception as exc:                      # keep the worker alive through a database hiccup
                    self.stderr.write(f"Error: {exc}")
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            self.stdout.write("Stopped.")
