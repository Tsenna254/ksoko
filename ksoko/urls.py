from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from market import accounts

urlpatterns = [
    # The admin's own login would skip email verification, so it redirects to the site's login.
    path(settings.ADMIN_URL + "login/", accounts.admin_login_redirect),
    path(settings.ADMIN_URL, admin.site.urls),
    path("", include("market.urls")),
]

# In development Django serves uploaded files. In production, let Nginx serve MEDIA_ROOT (see README).
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
