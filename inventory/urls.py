from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("centro/<int:pk>/", views.store_dashboard, name="store"),
    path("chiusura/", views.ClosingView.as_view(), name="closing"),
    path("chiusura/<int:pk>/elimina/", views.closing_entry_delete, name="closing_delete"),
    path("magazzino/", views.PackageListView.as_view(), name="list"),
    path("storico/", views.PackageListView.as_view(history=True), name="history"),
    path("nuovo/", views.PackageCreateView.as_view(), name="create"),
    path("lotti/", views.BatchListView.as_view(), name="batch_list"),
    path("lotti/nuovo/", views.BatchCreateView.as_view(), name="batch_create"),
    path("lotti/<int:pk>/modifica/", views.BatchUpdateView.as_view(), name="batch_update"),
    path("prezzo/", views.PriceUpdateView.as_view(), name="price"),
    path("pacco/<int:pk>/", views.PackageDetailView.as_view(), name="detail"),
    path("pacco/<int:pk>/modifica/", views.PackageUpdateView.as_view(), name="update"),
    path("pacco/<int:pk>/vendi/", views.PackageSellView.as_view(), name="sell"),
    path("pacco/<int:pk>/ripristina/", views.package_restore, name="restore"),
    path("pacco/<int:pk>/elimina/", views.PackageDeleteView.as_view(), name="delete"),
    path("pacco/<int:pk>/img/<str:kind>/", views.package_image, name="image"),
]
