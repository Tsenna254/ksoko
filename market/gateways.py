"""Thin clients for the M-Pesa (Daraja) and Flutterwave (card) APIs."""
import base64
import math

import requests
from django.conf import settings
from django.utils import timezone


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


def _mpesa_token():
    r = requests.get(
        _mpesa_base() + "/oauth/v1/generate", params={"grant_type": "client_credentials"},
        auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET), timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def mpesa_stk_push(phone, amount, reference):
    """Ask the customer's phone for their M-Pesa PIN. Returns the CheckoutRequestID."""
    stamp = timezone.localtime().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{stamp}".encode()).decode()
    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": stamp,
        "TransactionType": settings.MPESA_TRANSACTION_TYPE,
        "Amount": max(1, math.ceil(amount)),
        "PartyA": phone,
        "PartyB": settings.MPESA_SHORTCODE,
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
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise GatewayError("Couldn't reach M-Pesa. Please try again in a moment.") from exc
    if data.get("ResponseCode") == "0":
        return data["CheckoutRequestID"]
    raise GatewayError(data.get("errorMessage") or data.get("ResponseDescription") or "M-Pesa rejected the request.")


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
