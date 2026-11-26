"""Template tags for the Contact us bubble and the footer links.

Save as market/templatetags/contact_tags.py (create the templatetags folder, with an empty __init__.py, if it
doesn't exist). In base.html: {% load contact_tags %}, then {% contact_links %} in the footer and
{% contact_widget %} just before </body>.

Settings come from environment variables on Render:
    CONTACT_EMAIL       the public support address (shown in the footer; new messages are emailed here too)
    CONTACT_WHATSAPP    your WhatsApp number in international format, digits only, e.g. 2547XXXXXXXX
    CONTACT_NOTIFY_EMAIL (optional) send new-message alerts somewhere other than CONTACT_EMAIL
"""
import os
from urllib.parse import quote

from django import template
from django.conf import settings

register = template.Library()

WHATSAPP_GREETING = "Hello Ksoko, I need help with "


def _setting(name):
    return str(getattr(settings, name, "") or os.environ.get(name, "") or "").strip()


def _contact_details():
    number = "".join(ch for ch in _setting("CONTACT_WHATSAPP") if ch.isdigit())
    return {
        "contact_email": _setting("CONTACT_EMAIL"),
        "whatsapp_url": f"https://wa.me/{number}?text={quote(WHATSAPP_GREETING)}" if number else "",
    }


@register.inclusion_tag("contact/footer_links.html")
def contact_links():
    return _contact_details()


@register.inclusion_tag("contact/widget.html", takes_context=True)
def contact_widget(context):
    request = context.get("request")
    user = getattr(request, "user", None) or context.get("user")
    signed_in = bool(user is not None and user.is_authenticated)
    details = _contact_details()
    details.update({
        "csrf_token": context.get("csrf_token"),
        "signed_in": signed_in,
        "user_name": user.get_username() if signed_in else "",
        "user_email": (user.email or "") if signed_in else "",
        "has_thread": bool(getattr(request, "session", None) and request.session.get("support_thread")),
    })
    return details
