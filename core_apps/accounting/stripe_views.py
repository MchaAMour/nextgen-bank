import logging

import stripe
from django.core.exceptions import ImproperlyConfigured
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import exceptions, generics, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import StripeBalanceTransaction, StripeWebhookEvent
from .permissions import AccountingPermission
from .stripe_payments import process_event, verify_event
from .stripe_reconciliation import reconciliation_report, sync_balance_transactions
from .views import LedgerPagination

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


class StripeNotConfigured(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Stripe is not configured."
    default_code = "payment_provider_not_configured"


class StripeUnreachable(exceptions.APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "Stripe could not be reached; retry later."
    default_code = "payment_provider_error"


def call_stripe(function, **kwargs):
    try:
        return function(**kwargs)
    except ImproperlyConfigured:
        raise StripeNotConfigured()
    except stripe.StripeError:
        logger.exception("Stripe request failed")
        raise StripeUnreachable()


class BalanceTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StripeBalanceTransaction
        fields = ["id", "stripe_id", "reporting_category", "amount", "fee", "net", "currency", "stripe_status",
                  "stripe_created", "source_id", "state", "review_reason", "payment", "refund", "journal_entry"]


class BalanceTransactionList(generics.ListAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_stripebalancetransaction"]
    serializer_class = BalanceTransactionSerializer
    queryset = StripeBalanceTransaction.objects.order_by("-stripe_created", "stripe_id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["state", "reporting_category"]


class BalanceTransactionSync(APIView):
    """Fetch new Stripe balance transactions and post every unfinished one."""
    permission_classes = [AccountingPermission]
    write_permissions = {"POST": ["accounting.add_stripebalancetransaction", "accounting.add_journalentry"]}

    def post(self, request):
        return Response(call_stripe(sync_balance_transactions, actor=request.user))


class StripeReconciliation(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_stripebalancetransaction", "accounting.view_journalline"]

    def get(self, request):
        report = call_stripe(reconciliation_report)
        money = ("ledger_balance", "stripe_balance", "difference", "unexplained")
        report.update({key: str(report[key]) for key in money})
        report["needs_review"]["net"] = str(report["needs_review"]["net"])
        report["unsynced_payments"]["amount"] = str(report["unsynced_payments"]["amount"])
        return Response(report)
