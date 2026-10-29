import logging

import stripe
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import exceptions, generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend

from .models import (BASE_CURRENCY, AllocationRelease, CreditNote, Customer, CustomerPayment, Invoice, InvoiceLine, PaymentAllocation, PaymentRefund,
    StripeCheckoutSession)
from .permissions import AccountingPermission
from .services import (allocate_payment, audit, cancel_invoice, create_draft_invoice, delete_draft_invoice,
    invoice_aging, issue_credit_note, post_invoice, record_customer_payment, refund_payment,
    release_allocation, replace_draft_invoice)
from .stripe_payments import create_invoice_checkout
from .views import LedgerPagination

logger = logging.getLogger(__name__)


class StripeUnavailable(exceptions.APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The payment provider could not create a checkout session; retry later."
    default_code = "payment_provider_error"


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


def replay_aware(model, lookup, service, *args, **kwargs):
    """201 when the service creates a record; 200 when an identical retry returns the original."""
    replay = model.objects.filter(**lookup).exists()
    record = run_service(service, *args, **kwargs)
    return record, status.HTTP_200_OK if replay else status.HTTP_201_CREATED


def validated(serializer_class, data):
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)
    for nested in ("lines", "allocations"):
        if nested in data:
            data[nested] = [dict(item) for item in data[nested]]
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
        created, request = self.instance is None, self.context.get("request")
        with transaction.atomic():
            customer = run_service(super().save, **kwargs)
            audit("customer.created" if created else "customer.updated", customer, f"Customer {customer.name}",
                  actor=request.user if request else None, name=customer.name, email=customer.email,
                  is_active=customer.is_active)
        return customer


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
    currency = serializers.CharField(max_length=3, default=BASE_CURRENCY)
    lines = InvoiceLineSerializer(many=True, allow_empty=False)


class CancellationInput(serializers.Serializer):
    cancellation_date = serializers.DateField()


class ReleaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = AllocationRelease
        fields = ["id", "allocation", "released_on", "reason", "released_by"]


class AllocationSerializer(serializers.ModelSerializer):
    release = ReleaseSerializer(read_only=True, default=None)

    class Meta:
        model = PaymentAllocation
        fields = ["id", "payment", "invoice", "reference", "amount", "allocated_on", "release"]


class RefundSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentRefund
        fields = ["id", "payment", "reference", "amount", "refunded_on", "journal_entry"]


class PaymentSerializer(serializers.ModelSerializer):
    unapplied_amount = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    allocations = AllocationSerializer(many=True, read_only=True)
    refunds = RefundSerializer(many=True, read_only=True)

    class Meta:
        model = CustomerPayment
        fields = ["id", "customer", "reference", "amount", "currency", "received_on", "journal_entry",
                  "unapplied_amount", "allocations", "refunds"]


class AllocationInput(serializers.Serializer):
    invoice = serializers.PrimaryKeyRelatedField(queryset=Invoice.objects.all())
    amount = DecimalString(max_digits=18, decimal_places=2)
    reference = serializers.CharField(max_length=100)


class LaterAllocationInput(AllocationInput):
    allocated_on = serializers.DateField()


class PaymentInput(serializers.Serializer):
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    reference = serializers.CharField(max_length=100)
    amount = DecimalString(max_digits=18, decimal_places=2)
    currency = serializers.CharField(max_length=3)
    received_on = serializers.DateField()
    allocations = AllocationInput(many=True, default=list)


class ReleaseInput(serializers.Serializer):
    released_on = serializers.DateField()
    reason = serializers.CharField(max_length=500)


class RefundInput(serializers.Serializer):
    reference = serializers.CharField(max_length=100)
    amount = DecimalString(max_digits=18, decimal_places=2)
    refunded_on = serializers.DateField()


class CreditNoteSerializer(serializers.ModelSerializer):
    amount = DecimalString(max_digits=18, decimal_places=2)

    class Meta:
        model = CreditNote
        fields = ["id", "number", "invoice", "amount", "issued_on", "reason", "journal_entry"]
        read_only_fields = ["journal_entry"]
        # Retries reuse a number; the service decides between replay and conflict.
        extra_kwargs = {"number": {"validators": []}}


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
    required_permissions = ["accounting.view_invoice", "accounting.view_paymentallocation",
                            "accounting.view_creditnote"]
    write_permissions = {"POST": ["accounting.add_invoice"]}
    serializer_class = InvoiceSerializer
    queryset = Invoice.objects.with_balances().prefetch_related("lines").order_by("-issue_date", "id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["customer", "status", "currency"]

    def create(self, request, *args, **kwargs):
        data = validated(DraftInvoiceInput, request.data)
        invoice = run_service(create_draft_invoice, actor=request.user, **data)
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
        invoice = run_service(replace_draft_invoice, self.get_object(), actor=request.user,
                              **validated(DraftInvoiceInput, request.data))
        return Response(InvoiceDetailSerializer(invoice).data)

    def perform_destroy(self, instance):
        run_service(delete_draft_invoice, instance, actor=self.request.user)


class InvoiceAction(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = InvoiceList.required_permissions

    def invoice(self, pk):
        return get_object_or_404(Invoice, pk=pk)

    def respond(self, invoice):
        invoice = InvoiceList.queryset.get(pk=invoice.pk)
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


class CheckoutSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StripeCheckoutSession
        fields = ["id", "invoice", "stripe_session_id", "url", "amount", "currency", "status", "expires_at"]


class InvoiceCheckout(InvoiceAction):
    """Create (or reuse) a Stripe Checkout Session for the invoice's open balance."""
    write_permissions = {"POST": ["accounting.add_stripecheckoutsession"]}

    def post(self, request, pk):
        try:
            session = run_service(create_invoice_checkout, self.invoice(pk), actor=request.user)
        except stripe.StripeError:
            logger.exception("Stripe Checkout Session creation failed")
            raise StripeUnavailable()
        return Response(CheckoutSessionSerializer(session).data, status=status.HTTP_201_CREATED)


class PaymentList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_customerpayment", "accounting.view_paymentallocation",
                            "accounting.view_paymentrefund"]
    write_permissions = {"POST": ["accounting.add_customerpayment", "accounting.add_paymentallocation"]}
    serializer_class = PaymentSerializer
    queryset = CustomerPayment.objects.with_balances().prefetch_related(
        "allocations__release", "refunds").order_by("-received_on", "id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["customer", "currency"]

    def create(self, request, *args, **kwargs):
        data = validated(PaymentInput, request.data)
        payment, code = replay_aware(CustomerPayment, {"reference": data["reference"]},
                                     record_customer_payment, data.pop("customer"), actor=request.user, **data)
        return Response(PaymentSerializer(PaymentList.queryset.get(pk=payment.pk)).data, status=code)


class PaymentDetail(generics.RetrieveAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = PaymentList.required_permissions
    serializer_class = PaymentSerializer
    queryset = PaymentList.queryset


class PaymentAllocationCreate(APIView):
    """Apply a payment's unapplied credit to another invoice."""
    permission_classes = [AccountingPermission]
    required_permissions = PaymentList.required_permissions
    write_permissions = {"POST": ["accounting.add_paymentallocation"]}

    def post(self, request, pk):
        payment = get_object_or_404(CustomerPayment, pk=pk)
        data = validated(LaterAllocationInput, request.data)
        allocation, code = replay_aware(PaymentAllocation, {"reference": data["reference"]},
                                        allocate_payment, payment, actor=request.user, **data)
        return Response(AllocationSerializer(allocation).data, status=code)


class AllocationReleaseCreate(APIView):
    """Take an allocation back: the invoice is open and the payment's credit unapplied again."""
    permission_classes = [AccountingPermission]
    required_permissions = PaymentList.required_permissions
    write_permissions = {"POST": ["accounting.add_allocationrelease"]}

    def post(self, request, pk):
        allocation = get_object_or_404(PaymentAllocation, pk=pk)
        data = validated(ReleaseInput, request.data)
        release, code = replay_aware(AllocationRelease, {"allocation": allocation},
                                     release_allocation, allocation, actor=request.user, **data)
        return Response(ReleaseSerializer(release).data, status=code)


class PaymentRefundCreate(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = PaymentList.required_permissions
    write_permissions = {"POST": ["accounting.add_paymentrefund"]}

    def post(self, request, pk):
        payment = get_object_or_404(CustomerPayment, pk=pk)
        data = validated(RefundInput, request.data)
        refund, code = replay_aware(PaymentRefund, {"reference": data["reference"]},
                                    refund_payment, payment, actor=request.user, **data)
        return Response(RefundSerializer(refund).data, status=code)


class CreditNoteList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_creditnote"]
    write_permissions = {"POST": ["accounting.add_creditnote"]}
    serializer_class = CreditNoteSerializer
    queryset = CreditNote.objects.order_by("-issued_on", "id")
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["invoice"]

    def create(self, request, *args, **kwargs):
        data = validated(CreditNoteSerializer, request.data)
        note, code = replay_aware(CreditNote, {"number": data["number"]},
                                  issue_credit_note, data.pop("invoice"), actor=request.user, **data)
        return Response(CreditNoteSerializer(note).data, status=code)


class AgingQuery(serializers.Serializer):
    as_of = serializers.DateField(default=timezone.localdate)


class AgingReport(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = InvoiceList.required_permissions + ["accounting.view_customerpayment",
                                                               "accounting.view_paymentrefund"]

    def get(self, request):
        query = AgingQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        as_of = query.validated_data["as_of"]
        groups = invoice_aging(as_of=as_of)
        return Response({"as_of": as_of.isoformat(), "currencies": {
            currency: {bucket: format(amount, ".2f") for bucket, amount in amounts.items()}
            for currency, amounts in sorted(groups.items())}})
