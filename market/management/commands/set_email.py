from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from market.models import AccountSecurity


class Command(BaseCommand):
    help = "Set a user's email address and mark it verified (for staff accounts made with createsuperuser)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("email")

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError("No such user.")
        user.email = options["email"].strip().lower()
        user.save(update_fields=["email"])
        AccountSecurity.objects.update_or_create(user=user, defaults={"email_verified": True, "verified_at": timezone.now()})
        self.stdout.write(f"{user.username} now uses {user.email} (marked verified). Staff still get a code at every sign-in.")
