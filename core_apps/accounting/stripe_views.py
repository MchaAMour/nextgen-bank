import logging

import stripe
from django.core.exceptions import ImproperlyConfigured
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import StripeWebhookEvent
from .stripe_payments import process_event, verify_event

logger = logging.getLogger(__name__)


class StripeWebhook(APIView):
    """Stripe calls this without user credentials; the signature is the authentication."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def post(self, request):
        try:
            event = verify_event(request.body, request.META.get("HTTP_STRIPE_SIGNATURE"))
        except ImproperlyConfigured:
            logger.error("Stripe webhook received but STRIPE_WEBHOOK_SECRET is not configured")
            return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except (ValueError, stripe.SignatureVerificationError):
            return Response({"detail": "Invalid Stripe signature or payload."}, status=status.HTTP_400_BAD_REQUEST)
        record = process_event(event)
        if record.status == StripeWebhookEvent.Status.FAILED:
            logger.error("Stripe event %s failed: %s", record.event_id, record.error)
            # A non-2xx response makes Stripe retry the delivery.
            return Response({"status": record.status}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({"status": record.status})
