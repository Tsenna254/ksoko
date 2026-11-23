from django.core.management.base import BaseCommand

from market import maintenance


class Command(BaseCommand):
    help = "Run housekeeping now (the site also does this itself): campaigns, expired payments, old data, expired sessions."

    def handle(self, *args, **options):
        maintenance.run_minutely()
        maintenance.run_hourly()
        self.stdout.write("Maintenance done.")
