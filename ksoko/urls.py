from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path

from market import accounts
from market.media import serve_media

urlpatterns = [
    # The admin's own login would skip email verification, so it redirects to the site's login.
    path(settings.ADMIN_URL + "login/", accounts.admin_login_redirect),
    path(settings.ADMIN_URL, admin.site.urls),
    path("", include("market.urls")),
]

# In development Django serves uploaded files. In production, let Nginx serve MEDIA_ROOT (see README).
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif settings.SERVE_MEDIA_WITH_DJANGO:
    urlpatterns += [re_path(r"^media/(?P<path>.*)$", serve_media)]   # uploads on a persistent disk
