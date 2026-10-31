#!/usr/bin/env bash
# Runs on every Render deploy (render.yaml: buildCommand). Stops at the first problem.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

# Migration files are created on your computer and committed. Never generate them on the server.
if ! ls market/migrations/0001_*.py >/dev/null 2>&1; then
  echo "ERROR: market/migrations has no migration files."
  echo "On your computer run:  python manage.py makemigrations market"
  echo "then commit and push the new files in market/migrations/ and deploy again."
  exit 1
fi
python manage.py makemigrations --check --dry-run || {
  echo "ERROR: your models changed but the matching migration files aren't committed."
  echo "On your computer run:  python manage.py makemigrations market   then commit market/migrations/ and push."
  exit 1
}

python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py seed_categories

# First deploy only: create the admin account from environment variables (then delete the password variable).
if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ] && [ -n "$DJANGO_SUPERUSER_EMAIL" ]; then
  python manage.py createsuperuser --noinput || echo "Admin account already exists, skipping."
  python manage.py set_email "$DJANGO_SUPERUSER_USERNAME" "$DJANGO_SUPERUSER_EMAIL"
fi
