from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import exceptions, generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend

from .models import Customer, Invoice, InvoiceLine, InvoiceReceipt
from .permissions import AccountingPermission
from .services import (cancel_invoice, create_draft_invoice, delete_draft_invoice, invoice_aging,
    post_invoice, receive_invoice_payment, replace_draft_invoice)
from .views import LedgerPagination


class Conflict(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with a concurrent change; retry it."
    default_code = "conflict"


def run_service(service, *args, **kwargs):
    """Map service validation to 400 and database conflicts (such as concurrent reuse) to 409."""
    try:
        return service(*args, **kwargs)
    except DjangoValidationError as error:
        raise exceptions.ValidationError(
            error.message_dict if hasattr(error, "error_dict") else error.messages)
    except IntegrityError:
        raise Conflict()


def validated(serializer_class, data):
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)
    if "lines" in data:
        data["lines"] = [dict(line) for line in data["lines"]]
    return data


class DecimalString(serializers.DecimalField):
    """Amounts arrive as strings; JSON numbers are binary floats."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError("Send amounts as decimal strings.")
        return super().to_internal_value(data)


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "email", "is_active"]

    def save(self, **kwargs):
        return run_service(super().save, **kwargs)


class InvoiceSerializer(serializers.ModelSerializer):
    open_balance = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    total = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = ["id", "number", "customer", "issue_date", "due_date", "currency",
                  "status", "total", "posted_total", "open_balance", "journal_entry", "cancellation_entry"]


class InvoiceLineSerializer(serializers.ModelSerializer):
    quantity = DecimalString(max_digits=10, decimal_places=3)
    unit_price = DecimalString(max_digits=12, decimal_places=2)
    total = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = InvoiceLine
        fields = ["id", "description", "quantity", "unit_price", "total"]


class InvoiceDetailSerializer(InvoiceSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)

    class Meta(InvoiceSerializer.Meta):
        fields = InvoiceSerializer.Meta.fields + ["lines"]


class DraftInvoiceInput(serializers.Serializer):
    """Full draft document; the services validate lifecycle and business rules."""
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    number = serializers.CharField(max_length=40)
    issue_date = serializers.DateField()
    due_date = serializers.DateField()
    currency = serializers.CharField(max_length=3, default="USD")
    lines = InvoiceLineSerializer(many=True, allow_empty=False)


class CancellationInput(serializers.Serializer):
    cancellation_date = serializers.DateField()


class ReceiptSerializer(serializers.ModelSerializer):
    amount = DecimalString(max_digits=18, decimal_places=2)

    class Meta:
        model = InvoiceReceipt
        fields = ["id", "invoice", "reference", "amount", "received_on", "journal_entry"]
        read_only_fields = ["journal_entry"]
        # Replays reuse a reference; the service decides between replay and conflict.
        extra_kwargs = {"reference": {"validators": []}}


class CustomerList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_customer"]
    write_permissions = {"POST": ["accounting.add_customer"]}
    serializer_class = CustomerSerializer
    queryset = Customer.objects.order_by("name", "id")
    pagination_class = LedgerPagination


class CustomerDetail(generics.RetrieveUpdateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = CustomerList.required_permissions
    write_permissions = {method: ["accounting.change_customer"] for method in ("PUT", "PATCH")}
    serializer_class = CustomerSerializer
    queryset = Customer.objects.all()


class InvoiceList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_invoice", "accounting.view_invoicereceipt"]
    write_permissions = {"POST": ["accounting.add_invoice"]}
    serializer_class = InvoiceSerializer
    queryset = Invoice.objects.prefetch_related("lines").order_by("-issue_date", "id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["customer", "status", "currency"]

    def create(self, request, *args, **kwargs):
        data = validated(DraftInvoiceInput, request.data)
        invoice = run_service(create_draft_invoice, **data)
        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)


class InvoiceDetail(generics.RetrieveUpdateDestroyAPIView):
    """PUT replaces a draft's header and lines; DELETE removes a draft."""
    permission_classes = [AccountingPermission]
    required_permissions = InvoiceList.required_permissions
    write_permissions = {"PUT": ["accounting.change_invoice"], "DELETE": ["accounting.delete_invoice"]}
    http_method_names = ["get", "put", "delete", "head", "options"]
    serializer_class = InvoiceDetailSerializer
    queryset = InvoiceList.queryset

    def update(self, request, *args, **kwargs):
        invoice = run_service(replace_draft_invoice, self.get_object(),
                              **validated(DraftInvoiceInput, request.data))
        return Response(InvoiceDetailSerializer(invoice).data)

    def perform_destroy(self, instance):
        run_service(delete_draft_invoice, instance)


class InvoiceAction(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = InvoiceList.required_permissions

    def invoice(self, pk):
        return get_object_or_404(Invoice, pk=pk)

    def respond(self, invoice):
        invoice = Invoice.objects.prefetch_related("lines").get(pk=invoice.pk)
        return Response(InvoiceDetailSerializer(invoice).data)


class InvoicePost(InvoiceAction):
    write_permissions = {"POST": ["accounting.post_invoice"]}

    def post(self, request, pk):
        return self.respond(run_service(post_invoice, self.invoice(pk), actor=request.user))


class InvoiceCancel(InvoiceAction):
    write_permissions = {"POST": ["accounting.cancel_invoice"]}

    def post(self, request, pk):
        data = validated(CancellationInput, request.data)
        return self.respond(run_service(cancel_invoice, self.invoice(pk), actor=request.user, **data))


class ReceiptList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_invoicereceipt"]
    write_permissions = {"POST": ["accounting.add_invoicereceipt"]}
    serializer_class = ReceiptSerializer
    queryset = InvoiceReceipt.objects.order_by("-received_on", "id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["invoice"]

    def create(self, request, *args, **kwargs):
        """201 for a new receipt; 200 when an identical request replays an existing one."""
        data = validated(ReceiptSerializer, request.data)
        replay = InvoiceReceipt.objects.filter(reference=data["reference"]).exists()
        receipt = run_service(receive_invoice_payment, data.pop("invoice"), actor=request.user, **data)
        return Response(ReceiptSerializer(receipt).data,
                        status=status.HTTP_200_OK if replay else status.HTTP_201_CREATED)


class AgingQuery(serializers.Serializer):
    as_of = serializers.DateField(default=timezone.localdate)


class AgingReport(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = InvoiceList.required_permissions

    def get(self, request):
        query = AgingQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        as_of = query.validated_data["as_of"]
        groups = invoice_aging(as_of=as_of)
        return Response({"as_of": as_of.isoformat(), "currencies": {
            currency: {bucket: format(amount, ".2f") for bucket, amount in amounts.items()}
            for currency, amounts in sorted(groups.items())}})
