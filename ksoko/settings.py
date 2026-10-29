import os
import urllib.parse
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Read KEY=value lines from a .env file (for local development). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _flag(name, default="0"):
    return os.environ.get(name, default) == "1"


# ---- Core, and refusing to start with unsafe production settings -------------------------
DEBUG = _flag("DJANGO_DEBUG")          # OFF unless you ask for it. Local development: DJANGO_DEBUG=1 in .env

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY to a long random value (see README).")
    SECRET_KEY = "dev-only-change-me"
elif not DEBUG and (len(SECRET_KEY) < 40 or SECRET_KEY == "dev-only-change-me"):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is too short or is the sample value. Generate a new one.")

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "").split(",") if h.strip()]
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Set DJANGO_ALLOWED_HOSTS to your domain, e.g. shop.example.com.")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Separate secret for hashing IP addresses and session keys, so leaking one key doesn't expose the other.
IP_HASH_SALT = os.environ.get("IP_HASH_SALT") or SECRET_KEY

# The Django admin lives here. Change it (e.g. "backstage-x7k/") so bots can't find it.
ADMIN_URL = os.environ.get("ADMIN_URL", "admin/").strip("/") + "/"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "market",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "market.middleware.AccountGuardMiddleware",
    "market.middleware.TrafficMiddleware",
    "market.middleware.MaintenanceMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ksoko.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "market.context_processors.categories",
                "market.context_processors.cart",
                "market.context_processors.site",
            ],
        },
    },
]

WSGI_APPLICATION = "ksoko.wsgi.application"


# ---- Database: PostgreSQL in production ---------------------------------------------------
def _database():
    url = os.environ.get("DATABASE_URL", "")
    if url:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("postgres", "postgresql"):
            raise ImproperlyConfigured("DATABASE_URL must look like postgres://user:password@host:5432/dbname")
        return {
            "ENGINE": "django.db.backends.postgresql", "NAME": u.path.lstrip("/"),
            "USER": urllib.parse.unquote(u.username or ""), "PASSWORD": urllib.parse.unquote(u.password or ""),
            "HOST": u.hostname or "", "PORT": u.port or "", "CONN_MAX_AGE": 60,
        }
    if not DEBUG and not _flag("ALLOW_SQLITE_IN_PRODUCTION"):
        # SQLite cannot lock rows, so two shoppers could buy the last item, or two bids could collide.
        raise ImproperlyConfigured("Set DATABASE_URL to a PostgreSQL database for production (see README).")
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}


DATABASES = {"default": _database()}

# Shared cache for rate limiting. Without REDIS_URL each server process counts separately.
if os.environ.get("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": os.environ["REDIS_URL"]}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Customer uploads are stored here. In production serve /media/ from Nginx with the headers in the README.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

# ---- Upload limits -----------------------------------------------------------------------
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 20
MAX_LISTING_IMAGES = 6
MAX_IMAGE_MB = 5
MAX_PROMO_IMAGE_MB = 5
MAX_PROMO_VIDEO_MB = 25

# ---- Cookies, HTTPS and browser protections -----------------------------------------------
TRUST_PROXY_HEADERS = _flag("TRUST_PROXY_HEADERS")   # only behind a proxy/load balancer you control
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "1") == "1"
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    if TRUST_PROXY_HEADERS:
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Staff sessions end after this long without activity.
STAFF_SESSION_SECONDS = 2 * 60 * 60

# ---- Email (verification codes) -----------------------------------------------------------
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
    EMAIL_TIMEOUT = 15
elif DEBUG:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"   # codes are printed in the terminal
else:
    raise ImproperlyConfigured("Set EMAIL_HOST (and EMAIL_HOST_USER / EMAIL_HOST_PASSWORD): accounts need email codes.")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Ksoko <no-reply@localhost>")

# Email queue: every code and reset email is queued, sent in the background and retried on failure.
EMAIL_SEND_ASYNC = _flag("EMAIL_SEND_ASYNC", "1")    # send right away from a background thread
EMAIL_MAX_ATTEMPTS = 5
EMAIL_RETRY_SECONDS = (30, 120, 300, 900)            # wait before retry 1, 2, 3, then 4 onwards
EMAIL_SEND_BATCH = 25                                # emails handled per run
EMAIL_OUTBOX_RETENTION_DAYS = 14                     # delivery records are deleted after this long

PASSWORD_RESET_TIMEOUT = 60 * 60   # password reset links work for one hour
CODE_TTL_MINUTES = 10          # how long an emailed code works
CODE_MAX_ATTEMPTS = 5          # wrong guesses allowed per code
CODE_RESEND_SECONDS = 60       # wait between emails
CODE_MAX_PER_HOUR = 5          # emails per account per hour

# ---- Payments -----------------------------------------------------------------------------
# Test mode shows a "simulate payment" button. It can only run in DEBUG.
PAYMENTS_TEST_MODE = _flag("PAYMENTS_TEST_MODE", "1" if DEBUG else "0")
if PAYMENTS_TEST_MODE and not DEBUG:
    raise ImproperlyConfigured("PAYMENTS_TEST_MODE must be off in production: it lets anyone mark a payment as paid.")
PAYMENT_HOLD_MINUTES = 60      # unpaid payments are cancelled and stock released after this long

MPESA_ENV = os.environ.get("MPESA_ENV", "sandbox")
MPESA_CONSUMER_KEY = os.environ.get("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE = os.environ.get("MPESA_SHORTCODE", "")
MPESA_PASSKEY = os.environ.get("MPESA_PASSKEY", "")
MPESA_TRANSACTION_TYPE = os.environ.get("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline")
# The callback URL is SITE_URL + /payments/mpesa/callback/<MPESA_CALLBACK_SECRET>/ (a long random string only you and Safaricom know).
MPESA_CALLBACK_SECRET = os.environ.get("MPESA_CALLBACK_SECRET", "")
# Optional: only accept callbacks from these IPs (comma-separated; take them from Safaricom's Daraja documentation).
MPESA_CALLBACK_IPS = [i.strip() for i in os.environ.get("MPESA_CALLBACK_IPS", "").split(",") if i.strip()]
MPESA_MANUAL_KIND = os.environ.get("MPESA_MANUAL_KIND", "Paybill")
MPESA_MANUAL_NUMBER = os.environ.get("MPESA_MANUAL_NUMBER", "")

FLUTTERWAVE_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "")
SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:8000")

# ---- Personalisation and data retention -----------------------------------------------------
SEARCH_HISTORY_DAYS = 90
PAGEVIEW_RETENTION_DAYS = 180
