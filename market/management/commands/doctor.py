import io
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from market.models import SiteSettings


class Command(BaseCommand):
    help = ("Checks the setup (database, migrations, static files, cache, email, uploads) and says what to fix. "
            "Exits with an error only for problems that would make the site return errors.")

    def handle(self, *args, **options):
        self.failed = []
        out = self.stdout.write

        def ok(text):
            out(self.style.SUCCESS(f"  PASS  {text}"))

        def warn(text):
            out(self.style.WARNING(f"  WARN  {text}"))

        def fail(text):
            self.failed.append(text)
            out(self.style.ERROR(f"  FAIL  {text}"))

        out("Ksoko setup check")
        out(f"  Mode: {'development (DEBUG on)' if settings.DEBUG else 'production'}")

        # Database
        try:
            connection.ensure_connection()
            ok(f"Database reachable ({connection.vendor})")
            db_ok = True
        except Exception as exc:
            fail(f"Can't reach the database: {str(exc)[:200]}  -> check DATABASE_URL")
            db_ok = False

        if db_ok:
            # Migrations applied?
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if plan:
                names = ", ".join(str(step[0]) for step in plan[:4])
                fail(f"{len(plan)} migration(s) not applied ({names}...)  -> run: python manage.py migrate")
            else:
                ok("All migrations applied")
            # Model changes without a migration file?
            try:
                call_command("makemigrations", "--check", "--dry-run", stdout=io.StringIO(), stderr=io.StringIO())
                ok("Migration files match the models")
            except SystemExit:
                fail("Models have changed but migration files are missing  -> run: python manage.py makemigrations market, then commit market/migrations/")
            # Tables exist?
            existing = set(connection.introspection.table_names())
            missing = [m._meta.db_table for m in apps.get_app_config("market").get_models() if m._meta.db_table not in existing]
            if missing:
                fail(f"Missing database tables: {', '.join(missing[:5])}  -> run: python manage.py migrate")
            elif not plan:
                try:
                    SiteSettings.load()
                    ok("Site settings row ready")
                except Exception as exc:
                    fail(f"Can't read site settings: {str(exc)[:150]}")
            if not missing:
                if get_user_model().objects.filter(is_superuser=True).exists():
                    ok("An admin account exists")
                else:
                    warn("No admin account yet  -> python manage.py createsuperuser, then python manage.py set_email NAME EMAIL")

        # Static files
        if settings.DEBUG:
            ok("Static files: development mode serves them directly")
        else:
            manifest = Path(settings.STATIC_ROOT) / "staticfiles.json"
            if manifest.exists() and (Path(settings.STATIC_ROOT) / "css").exists():
                ok("Static files collected")
            else:
                fail("Static files not collected  -> run: python manage.py collectstatic --noinput (the build script does this)")

        # Cache (rate limits)
        try:
            cache.set("doctor-check", "1", 10)
            if cache.get("doctor-check") == "1":
                ok(f"Cache works ({'Redis' if 'redis' in settings.CACHES['default']['BACKEND'].lower() else 'in-memory: rate limits are per process'})")
            else:
                warn("Cache didn't return what was stored  -> rate limiting may not work")
        except Exception as exc:
            warn(f"Cache unreachable ({str(exc)[:120]})  -> check REDIS_URL. Rate limiting is off until it works.")

        # Uploads
        if settings.USE_S3:
            try:
                default_storage.exists("doctor-check.txt")
                ok("Upload storage (S3-compatible bucket) reachable")
            except Exception as exc:
                warn(f"Can't reach the upload bucket ({str(exc)[:120]})  -> check the AWS_* settings")
        else:
            media = Path(settings.MEDIA_ROOT)
            if media.exists():
                try:
                    probe = media / ".doctor"
                    probe.write_text("ok")
                    probe.unlink()
                    ok(f"Uploads folder writable ({media})")
                except OSError as exc:
                    warn(f"Uploads folder {media} isn't writable ({exc})")
            else:
                warn(f"Uploads folder {media} doesn't exist yet. Fine during a build; at runtime it needs the persistent disk mounted")

        # Email
        backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]
        if "console" in backend or "locmem" in backend:
            warn("Email is printed to the log, not sent  -> set EMAIL_HOST (or EMAIL_BACKEND) so people can get their codes")
        else:
            ok(f"Email configured ({backend})")

        out("")
        if self.failed:
            raise CommandError(f"{len(self.failed)} problem(s) would make the site fail. Fix the FAIL lines above.")
        out(self.style.SUCCESS("Everything essential is in place."))
