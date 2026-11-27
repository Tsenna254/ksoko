"""Thin clients for the M-Pesa (Daraja) and Flutterwave (card) APIs."""
import base64
import logging
import math
import os
import re

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    pass


# ---- M-Pesa -----------------------------------------------------------------

def mpesa_stk_ready():
    return all([settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET, settings.MPESA_SHORTCODE,
                settings.MPESA_PASSKEY, settings.MPESA_CALLBACK_SECRET])


def _callback_url():
    from django.urls import reverse
    return settings.SITE_URL.rstrip("/") + reverse("mpesa_callback", args=[settings.MPESA_CALLBACK_SECRET])


def _mpesa_base():
    return "https://api.safaricom.co.ke" if settings.MPESA_ENV == "production" else "https://sandbox.safaricom.co.ke"


def normalize_phone(raw):
    """Safaricom wants 2547XXXXXXXX or 2541XXXXXXXX: no +, spaces or leading zero. Safe to call on a number already in that form."""
    digits = re.sub(r"\D", "", str(raw or ""))
    match = re.fullmatch(r"(?:254|0)?([17]\d{8})", digits)
    if not match:
        raise GatewayError("Enter a valid Safaricom number, for example 0712345678.")
    return "254" + match.group(1)


def _masked(phone):
    return phone[:5] + "****" + phone[-2:]


def _party_b():
    """The number that receives the money. For a Paybill it is the shortcode. For a Till (Buy Goods) set MPESA_PARTY_B
    to the till number, and keep MPESA_SHORTCODE as the store number Safaricom gave you for the password."""
    return getattr(settings, "MPESA_PARTY_B", "") or os.environ.get("MPESA_PARTY_B", "").strip() or settings.MPESA_SHORTCODE


def _mpesa_token():
    try:
        r = requests.get(
            _mpesa_base() + "/oauth/v1/generate", params={"grant_type": "client_credentials"},
            auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET), timeout=20,
        )
    except requests.RequestException as exc:
        raise GatewayError("Couldn't reach M-Pesa. Please try again in a moment.") from exc
    token = None
    if r.status_code == 200:
        try:
            token = r.json()["access_token"]
        except (ValueError, KeyError):
            token = None
    if not token:
        logger.error("M-Pesa sign-in failed (HTTP %s): %s. Check MPESA_ENV and the consumer key and secret.",
                     r.status_code, r.text[:200])
        raise GatewayError("M-Pesa is unavailable right now. Please try again later or use the Paybill/Till details.")
    return token


def _stamp_and_password():
    stamp = timezone.localtime().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{stamp}".encode()).decode()
    return stamp, password


def mpesa_stk_push(phone, amount, reference):
    """Ask the customer's phone for their M-Pesa PIN. Returns the CheckoutRequestID."""
    phone = normalize_phone(phone)
    stamp, password = _stamp_and_password()
    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": stamp,
        "TransactionType": settings.MPESA_TRANSACTION_TYPE,
        "Amount": max(1, math.ceil(amount)),
        "PartyA": phone,
        "PartyB": _party_b(),
        "PhoneNumber": phone,
        "CallBackURL": _callback_url(),
        "AccountReference": reference[:12],
        "TransactionDesc": "Ksoko order",
    }
    try:
        r = requests.post(
            _mpesa_base() + "/mpesa/stkpush/v1/processrequest", json=payload,
            headers={"Authorization": f"Bearer {_mpesa_token()}"}, timeout=25,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise GatewayError("Couldn't reach M-Pesa. Please try again in a moment.") from exc
    if data.get("ResponseCode") == "0" and data.get("CheckoutRequestID"):
        logger.info("M-Pesa prompt requested for %s to %s", reference, _masked(phone))
        return str(data["CheckoutRequestID"])
    logger.warning("M-Pesa rejected the STK request for %s (HTTP %s, code %s): %s", reference, r.status_code,
                   data.get("errorCode") or data.get("ResponseCode"),
                   data.get("errorMessage") or data.get("ResponseDescription"))
    raise GatewayError(data.get("errorMessage") or data.get("ResponseDescription") or "M-Pesa rejected the request.")


def mpesa_stk_query(checkout_id):
    """Ask Safaricom what became of a prompt. Returns "paid", "failed" or "pending" (pending also covers any doubt)."""
    stamp, password = _stamp_and_password()
    payload = {"BusinessShortCode": settings.MPESA_SHORTCODE, "Password": password, "Timestamp": stamp,
               "CheckoutRequestID": checkout_id}
    try:
        r = requests.post(
            _mpesa_base() + "/mpesa/stkpushquery/v1/query", json=payload,
            headers={"Authorization": f"Bearer {_mpesa_token()}"}, timeout=25,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise GatewayError("Couldn't reach M-Pesa.") from exc
    if data.get("errorCode") or "ResultCode" not in data:
        return "pending"                       # for example "the transaction is being processed"
    return "paid" if str(data["ResultCode"]) == "0" else "failed"


# ---- Card (Flutterwave hosted checkout) ----------------------------------------

def card_ready():
    return bool(settings.FLUTTERWAVE_SECRET_KEY)


def _flw_headers():
    return {"Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}"}


def card_checkout_link(payment, user, return_url):
    """Create a hosted card checkout and return the URL to send the customer to."""
    payload = {
        "tx_ref": payment.reference,
        "amount": str(payment.amount),
        "currency": "KES",
        "redirect_url": return_url,
        "payment_options": "card",
        "customer": {"email": user.email or f"{user.username}@example.com", "name": user.username},
        "customizations": {"title": "Ksoko", "description": f"Payment {payment.reference}"},
    }
    try:
        r = requests.post("https://api.flutterwave.com/v3/payments", json=payload, headers=_flw_headers(), timeout=25)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise GatewayError("Couldn't reach the card processor. Please try again in a moment.") from exc
    if data.get("status") == "success":
        return data["data"]["link"]
    raise GatewayError(data.get("message") or "The card processor rejected the request.")


def card_verify(transaction_id):
    """Fetch the transaction from Flutterwave (never trust the redirect alone)."""
    try:
        r = requests.get(
            f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify", headers=_flw_headers(), timeout=25
        )
        return r.json().get("data") or {}
    except (requests.RequestException, ValueError) as exc:
        raise GatewayError("Couldn't verify the card payment. Please contact support with your payment reference.") from exc
