from django.utils import timezone
from rest_framework import generics, serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend

from .models import Customer, Invoice, InvoiceReceipt
from .permissions import LedgerReadPermission
from .services import invoice_aging
from .views import LedgerPagination


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "email", "is_active"]


class InvoiceSerializer(serializers.ModelSerializer):
    open_balance = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    total = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = ["id", "number", "customer", "issue_date", "due_date", "currency",
                  "status", "total", "posted_total", "open_balance", "journal_entry", "cancellation_entry"]


class ReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceReceipt
        fields = ["id", "invoice", "reference", "amount", "received_on", "journal_entry"]


class CustomerList(generics.ListAPIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = ["accounting.view_customer"]
    serializer_class = CustomerSerializer
    queryset = Customer.objects.order_by("name", "id")
    pagination_class = LedgerPagination


class InvoiceList(generics.ListAPIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = ["accounting.view_invoice", "accounting.view_invoicereceipt"]
    serializer_class = InvoiceSerializer
    queryset = Invoice.objects.prefetch_related("lines").order_by("-issue_date", "id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["customer", "status", "currency"]


class ReceiptList(generics.ListAPIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = ["accounting.view_invoicereceipt"]
    serializer_class = ReceiptSerializer
    queryset = InvoiceReceipt.objects.order_by("-received_on", "id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["invoice"]


class AgingQuery(serializers.Serializer):
    as_of = serializers.DateField(default=timezone.localdate)


class AgingReport(APIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = InvoiceList.required_permissions

    def get(self, request):
        query = AgingQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        as_of = query.validated_data["as_of"]
        groups = invoice_aging(as_of=as_of)
        return Response({"as_of": as_of.isoformat(), "currencies": {
            currency: {bucket: format(amount, ".2f") for bucket, amount in amounts.items()}
            for currency, amounts in sorted(groups.items())}})
