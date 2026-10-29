from django.urls import path

from .payment_views import payment_cancelled, payment_success

urlpatterns = [
    path("success", payment_success, name="payment-success"),
    path("cancelled", payment_cancelled, name="payment-cancelled"),
]
