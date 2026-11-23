from django.conf import settings
from django.views.static import serve


def serve_media(request, path):
    """Serve uploaded files from MEDIA_ROOT with headers that stop them ever running as a web page."""
    response = serve(request, path, document_root=settings.MEDIA_ROOT)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; media-src 'self'; sandbox"
    response["Cache-Control"] = "public, max-age=86400"
    return response
