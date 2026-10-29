# Ksoko (Django)

A marketplace where signed-in customers can upload listings with photos, bid on
auctions, buy fixed-price items and keep a watchlist.

## Run it

```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
cp .env.example .env        # local settings; DJANGO_DEBUG=1 switches on development mode
pip install -r requirements.txt
python manage.py makemigrations market
python manage.py migrate
python manage.py seed_categories
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000/. Register an account, then use **Sell an item**.
The admin is at /admin/.

## How customer uploads work

| Piece | File | What it does |
|---|---|---|
| Storage | `ksoko/settings.py` | `MEDIA_ROOT` / `MEDIA_URL`, size and count limits |
| Model | `market/models.py` | `ListingImage.image` (`ImageField`) saved under `media/listings/YYYY/MM/<random>.ext`; the file is deleted with the row |
| Validation | `market/forms.py` | multi-file field, max 6 photos, 5 MB each, JPG/PNG/WebP only, Pillow confirms each file is a real image |
| Form view | `market/views.py` | `create_listing` / `edit_listing` pass `request.FILES` to the form; only the seller can edit or delete |
| Page | `templates/market/listing_form.html` + `static/css/ksoko.css` | The Sell an item page: drag-and-drop photos, cover picker, Buy it now / Auction switch, auction length, free-delivery toggle, live listing preview. Uses `enctype="multipart/form-data"`; the server re-checks everything the browser checks |

Sellers can add photos or tick "Remove" on existing ones when editing. Auction
length (1 to 10 days) is chosen when listing; the format and end time can't be
changed afterwards, and once an auction has bids its price is locked too.

## Auctions

Run this every minute (cron or Task Scheduler) so ended auctions are marked
sold to the highest bidder, or ended if nobody bid:

```bash
python manage.py close_auctions
```

## Before going live

- Set `DJANGO_DEBUG=0`, a real `DJANGO_SECRET_KEY` and `DJANGO_ALLOWED_HOSTS`.
- Serve `media/` from Nginx or object storage (Django only serves it when `DEBUG` is on), and run `collectstatic`.
- Switch SQLite to PostgreSQL.
- Payment is not built. "Buy it now" currently records the purchase only; add M-Pesa (Daraja) STK push and an order/shipping flow next.


## Admin dashboard, orders and ratings

- **Dashboard** at `/manage/` (staff accounts only; create one with `createsuperuser`). Pick **Daily / Weekly / Monthly / Annually** to see sales (KES), transactions, average order value, page views, unique visitors, new listings and members, with the change against the previous period. Charts, top pages, most viewed listings, top vendors and recent transactions sit below.
- **Traffic** is recorded by `market/middleware.py` (one row per successful HTML page load; bots, staff, admin and static files are skipped).
- **Transactions** are `Order` rows, created when someone uses Buy it now or when `close_auctions` closes an auction with bids. Payment is still not connected, so orders are recorded as completed.
- **Ratings**: after an order, the buyer rates the item and the seller (1 to 5 stars, optional comment) from **My orders**. Item ratings show on cards and listing pages, vendor ratings show next to every seller name and on `/seller/<username>/`, and the dashboard lists best and lowest rated products and vendors.
- Fixed-price listings now have a **quantity**; each purchase takes one and the listing shows as sold at zero. Listings with sales or bids can't be deleted.

After pulling these changes run `python manage.py makemigrations market && python manage.py migrate`.


## Cart, checkout and payments

- **Cart menu**: a Cart button with a live count sits in the header. Click it for a mini cart with a **Checkout** button, or open `/cart/` to change quantities. "Buy it now" adds the item and goes straight to checkout; "Add to cart" is on every card and listing page. Auctions you win appear in the cart too, ready to pay.
- **Checkout** (`/checkout/`) takes **M-Pesa** or **Card**. Stock is reserved while the payment is open and returned if it is cancelled, rejected or expires (run `python manage.py expire_payments` from cron).
- **Admin confirmation**: when a customer has paid, the payment shows up under **Payments awaiting confirmation** on `/manage/` (and in Django admin under Payments, with bulk Confirm / Reject actions). Orders only become **Completed**, and only count as sales and open the rating prompt, after a staff member confirms.

### M-Pesa
Pick one:
1. **STK push (Daraja)**: set `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, `MPESA_CALLBACK_URL` (public HTTPS, ending `/payments/mpesa/callback/`) and `MPESA_ENV=production` when live. Set `MPESA_TRANSACTION_TYPE=CustomerBuyGoodsOnline` if the shortcode is a Till. The customer gets a PIN prompt and the callback records their M-Pesa receipt.
2. **Manual Paybill/Till**: set `MPESA_MANUAL_KIND` (`Paybill` or `Till`) and `MPESA_MANUAL_NUMBER`. Customers pay it themselves and type the confirmation code. Check the code against your M-Pesa statement before confirming.

### Card
Cards go through Flutterwave's hosted checkout, so card numbers never touch this server. Set `FLW_SECRET_KEY` and `SITE_URL` (your public address). The return is verified server-side against Flutterwave before the payment is marked paid. Other Kenya-friendly processors (Pesapal, DPO, Paystack) can be swapped in by editing `market/gateways.py`.

### Trying it without gateway accounts
With `DEBUG` on, `PAYMENTS_TEST_MODE` is on: each payment page has a **Simulate a successful payment** button. Use it, then confirm the payment on `/manage/`. Switch test mode off (`PAYMENTS_TEST_MODE=0`) before going live.

New migrations are needed again: `python manage.py makemigrations market && python manage.py migrate`. Run `pip install -r requirements.txt` for `requests`.


## Branding, promotional media and campaigns (admin menu)

The staff menu (shown across `/manage/`) now has four tabs: **Dashboard**, **Branding**, **Promo media** and **Campaigns**.

- **Branding** (`/manage/branding/`): upload the **website logo** (replaces the text logo in the header and footer), set the homepage headline and text, a **homepage banner background**, and a **page background image** with an adjustable cover strength. Replaced images are deleted from disk.
- **Promo media** (`/manage/media/`): upload banner images (JPG, PNG, WebP, 5 MB) or short videos (MP4, WebM, 25 MB). Choose where each shows (homepage banners, or the top of listing pages), add an optional link, set the order, and hide or delete it any time.
- **Campaigns** (`/manage/campaigns/`): create a campaign with a name, banner, badge text, **discount %**, eligible categories, **max listings per seller**, **minimum seller rating**, whether entries need approval, start and end dates and a **join deadline**. Every parameter can be edited later; running discounts are re-priced when you save. Approve or reject seller entries on the campaign page. Drafts stay hidden until you tick Published.
- **Sellers**: a **Campaigns** link in the top menu opens `/my/campaigns/`, where sellers see open campaigns and their terms, join with selected fixed-price listings (with a price preview), track entry status and withdraw.
- While a campaign is live, joined listings show the campaign price with the old price crossed out and a badge. Their prices are locked, and put back exactly as the seller had them when the campaign ends, the entry is withdrawn or rejected, or the campaign is deleted. The site starts and ends discounts itself at most once a minute; `python manage.py sync_campaigns` does the same from cron if you prefer.

Run `python manage.py makemigrations market && python manage.py migrate` again after updating.

**Branding tips**: if an uploaded background looks faint, lower **Cover strength** (page background) or **Banner tint** (homepage photo). Saving now tells you exactly what changed, or that nothing was saved and why. After updating, run `makemigrations` and `migrate` (a new "banner tint" setting was added), then re-save Branding once.


## Suggestions from search history

`market/recommend.py` powers **Recommended for you** on the homepage, **You may also like** on listing pages and **You might like** on empty search results. Its docstring explains the scoring in full.

- **Who is tracked**: logged-in users by user id; guests by an anonymous visitor id kept in their session (it carries over when they log in). The **IP address** is only a weak fallback for guests with almost no history: it is stored as a salted hash (never the raw address), counts a quarter as much, and only borrows other *anonymous* searches, because many people share one IP (offices, mobile networks).
- **Signals**: searches, listing views, watchlist, cart and purchases, with older activity fading over about two weeks. Visitors with no history see what is trending.
- **Privacy controls** on the homepage section: *Clear my history* and *Turn off personalisation*. Tell shoppers about this in your privacy policy (the Kenya Data Protection Act applies to IP addresses and browsing data). Run `python manage.py prune_search_history` from cron to delete data older than 90 days.
- If the site runs behind a proxy or load balancer you control, set `TRUST_PROXY_HEADERS=1` so the real visitor IP is used.
- **Admin**: the dashboard now lists top searches and searches that returned nothing, which shows you what to stock.

Run `makemigrations market` and `migrate` again after updating.


## Email verification and sign-in codes

- **Registering**: the new account must enter a 6-digit code emailed to it before it can sign in. Accounts that never confirm are deleted after a day.
- **Signing in from a new network (IP address)**: a fresh code is emailed first. Networks are remembered as keyed hashes, never raw IPs, after a code is accepted. Staff accounts get a code on **every** sign-in.
- Codes last 10 minutes, work once, allow 5 wrong tries, and at most 5 emails an hour per account. Only a hash of each code is stored. Sign-in codes only work from the network that asked for them.
- In development, emails are printed in the terminal. In production set `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `DEFAULT_FROM_EMAIL` (any SMTP service: your host's, Zoho, Brevo, SendGrid, Gmail app password).
- Staff made with `createsuperuser` must have an email: `python manage.py set_email USERNAME EMAIL` (this also marks it verified).
- Mobile and home connections change IP often, so people may be asked for a code more than on a fixed office network.

## Security changes and the going-live checklist

Fixed in code: session keys are no longer stored (only a hash; run `python manage.py scrub_session_keys` once if you ran an older version); uploads are saved with a safe extension and videos are checked by content; the site refuses to start in production with `DEBUG` on, a weak secret key, SQLite, test payments, or no email; the M-Pesa callback needs a secret URL token (and optional IP list); rate limits on sign-in, sign-up, codes, search, bids and payment codes; the Django admin login now goes through email verification, staff sessions end after 2 hours idle, and the admin URL can be moved; staff can't confirm unpaid payments without a written reason; every confirm/reject, staff sign-in, branding change and campaign decision is kept in an audit log (Django admin > Audit logs); campaign prices are locked from the moment a seller joins; unpaid payments expire automatically; old page views, searches, codes and sessions are deleted automatically.

**Before you go live, you still need to do these (they are server settings, not code):**
1. Copy `.env.example` to `.env` (or set real environment variables) and fill in: `DJANGO_SECRET_KEY`, `IP_HASH_SALT`, `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SITE_URL`, `DATABASE_URL` (PostgreSQL), `REDIS_URL` (so rate limits are shared between workers), email settings, `ADMIN_URL`, and `TRUST_PROXY_HEADERS=1` if behind Nginx or a load balancer. `DJANGO_DEBUG` must NOT be 1.
2. Serve everything over HTTPS (Let's Encrypt). Run `python manage.py collectstatic`, `migrate`, `createsuperuser`, then `set_email`.
3. Serve `/media/` from Nginx, never through Django, with these headers so an uploaded file can never run as a web page:
```
location /media/ {
    alias /path/to/ksoko_django/media/;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Content-Security-Policy "default-src 'none'; img-src 'self'; media-src 'self'; sandbox" always;
}
client_max_body_size 30m;
```
4. Add an M-Pesa callback secret (`MPESA_CALLBACK_SECRET`, a long random string) and register `SITE_URL + /payments/mpesa/callback/<secret>/` with Safaricom.
5. Keep `.env`, the database and backups out of git and web-readable folders; back up the database; keep Django, Pillow and other packages updated (`pip install -U -r requirements.txt` and `pip install pip-audit && pip-audit`).
6. Publish a privacy policy that covers email, IP address and browsing data (Kenya Data Protection Act).


## Forgotten password

"Forgot your password?" on the sign-in page (`/accounts/password-reset/`) emails a one-time link that works for one hour. The answer is the same whether or not the address has an account, requests are rate limited per network and per address, and the new password must pass the same strength rules as registration. After a reset every signed-in session is ended, remembered networks are cleared (so the next sign-in needs an emailed code) and the reset is recorded in the audit log. Because the link goes to the account's email, protect that mailbox with its own two-step sign-in.


## Automated email system

All verification-code emails (sign-up and new-network sign-in) and password-reset emails go through one queue (`market/emailer.py`):

- **Instant for the visitor**: the page answers immediately and the email is sent in the background, usually within a second or two. The verify page shows "Sending…" and warns if delivery failed.
- **Automatic retries**: a failed send is retried after 30 seconds, 2 minutes, 5 minutes and 15 minutes, then marked failed. Brief mail-server outages therefore fix themselves.
- **Never sends stale codes**: a code that has already expired is skipped, and requesting a new code cancels any older one still waiting.
- **Nothing sensitive is kept**: the message text (code or reset link) is wiped from the database as soon as the email is sent, fails, expires or is replaced. Delivery records (who, when, status, error) are deleted after 14 days.
- **Self-running**: a background thread sends right after queueing, and the site sweeps for leftovers and retries every minute. For extra reliability run a tiny worker next to the site: `python manage.py send_queued_emails --loop` (a systemd service works well), or run `python manage.py send_queued_emails` from cron every minute. Several workers can run together safely.
- **Monitoring**: the admin dashboard has an **Email delivery** panel for the last 24 hours (sent, waiting, failed, latest errors) that turns red when emails are failing. Full records are under Django admin > Outbound emails.
- Tests for the queue: `python manage.py test market`.

Run `makemigrations market` and `migrate` after updating.
