from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    # Il logout accetta solo POST (protetto da CSRF): un link non può disconnetterti.
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("inventory.urls")),
]
