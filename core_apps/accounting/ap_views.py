from django.db import transaction
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .ap_services import (allocate_supplier_payment, approve_bill, bill_aging, cancel_bill, create_draft_bill,
    delete_draft_bill, post_bill, record_supplier_payment, replace_draft_bill, supplier_statement)
from .ar_views import AgingQuery, CancellationInput, DecimalString, replay_aware, run_service, validated
from .models import BASE_CURRENCY, Bill, BillLine, Supplier, SupplierPayment, SupplierPaymentAllocation
from .permissions import AccountingPermission
from .report_views import period
from .services import audit
from .views import LedgerPagination, report_json


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["id", "name", "email", "is_active"]

    def save(self, **kwargs):
        created, request = self.instance is None, self.context.get("request")
        with transaction.atomic():
            supplier = run_service(super().save, **kwargs)
            audit("supplier.created" if created else "supplier.updated", supplier, f"Supplier {supplier.name}",
                  actor=request.user if request else None, name=supplier.name, email=supplier.email,
                  is_active=supplier.is_active)
        return supplier


class BillLineSerializer(serializers.ModelSerializer):
    account = serializers.CharField(source="account.code", max_length=20)
    quantity = DecimalString(max_digits=10, decimal_places=3)
    unit_price = DecimalString(max_digits=12, decimal_places=2)
    total = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = BillLine
        fields = ["id", "account", "description", "quantity", "unit_price", "total"]


class BillSerializer(serializers.ModelSerializer):
    open_balance = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    total = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = Bill
        fields = ["id", "supplier", "number", "bill_date", "due_date", "currency", "status", "total",
                  "posted_total", "open_balance", "created_by", "approved_by", "approved_at", "journal_entry",
                  "cancellation_entry"]


class BillDetailSerializer(BillSerializer):
    lines = BillLineSerializer(many=True, read_only=True)

    class Meta(BillSerializer.Meta):
        fields = BillSerializer.Meta.fields + ["lines"]


class BillLineInput(serializers.Serializer):
    account = serializers.CharField(max_length=20)
    description = serializers.CharField(max_length=500)
    quantity = DecimalString(max_digits=10, decimal_places=3)
    unit_price = DecimalString(max_digits=12, decimal_places=2)


class DraftBillInput(serializers.Serializer):
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all())
    number = serializers.CharField(max_length=60)
    bill_date = serializers.DateField()
    due_date = serializers.DateField()
    currency = serializers.CharField(max_length=3, default=BASE_CURRENCY)
    lines = BillLineInput(many=True, allow_empty=False)


class SupplierAllocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierPaymentAllocation
        fields = ["id", "payment", "bill", "reference", "amount", "allocated_on"]


class SupplierPaymentSerializer(serializers.ModelSerializer):
    unapplied_amount = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    allocations = SupplierAllocationSerializer(many=True, read_only=True)

    class Meta:
        model = SupplierPayment
        fields = ["id", "supplier", "reference", "amount", "currency", "paid_on", "journal_entry",
                  "unapplied_amount", "allocations"]


class SupplierAllocationInput(serializers.Serializer):
    bill = serializers.PrimaryKeyRelatedField(queryset=Bill.objects.all())
    amount = DecimalString(max_digits=18, decimal_places=2)
    reference = serializers.CharField(max_length=100)


class LaterSupplierAllocationInput(SupplierAllocationInput):
    allocated_on = serializers.DateField()


class SupplierPaymentInput(serializers.Serializer):
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all())
    reference = serializers.CharField(max_length=100)
    amount = DecimalString(max_digits=18, decimal_places=2)
    currency = serializers.CharField(max_length=3, default=BASE_CURRENCY)
    paid_on = serializers.DateField()
    allocations = SupplierAllocationInput(many=True, default=list)


class SupplierList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_supplier"]
    write_permissions = {"POST": ["accounting.add_supplier"]}
    serializer_class = SupplierSerializer
    queryset = Supplier.objects.order_by("name", "id")
    pagination_class = LedgerPagination


class SupplierDetail(generics.RetrieveUpdateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = SupplierList.required_permissions
    write_permissions = {method: ["accounting.change_supplier"] for method in ("PUT", "PATCH")}
    serializer_class = SupplierSerializer
    queryset = Supplier.objects.all()


BILLS = Bill.objects.with_balances().prefetch_related("lines__account").order_by("-bill_date", "id")


class BillList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_bill", "accounting.view_supplierpaymentallocation"]
    write_permissions = {"POST": ["accounting.add_bill"]}
    serializer_class = BillSerializer
    queryset = BILLS
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["supplier", "status", "currency"]

    def create(self, request, *args, **kwargs):
        bill = run_service(create_draft_bill, actor=request.user, **validated(DraftBillInput, request.data))
        return Response(BillDetailSerializer(BILLS.get(pk=bill.pk)).data, status=status.HTTP_201_CREATED)


class BillDetail(generics.RetrieveUpdateDestroyAPIView):
    """PUT replaces a draft's header and lines; DELETE removes a draft."""
    permission_classes = [AccountingPermission]
    required_permissions = BillList.required_permissions
    write_permissions = {"PUT": ["accounting.change_bill"], "DELETE": ["accounting.delete_bill"]}
    http_method_names = ["get", "put", "delete", "head", "options"]
    serializer_class = BillDetailSerializer
    queryset = BILLS

    def update(self, request, *args, **kwargs):
        bill = run_service(replace_draft_bill, self.get_object(), actor=request.user,
                           **validated(DraftBillInput, request.data))
        return Response(BillDetailSerializer(BILLS.get(pk=bill.pk)).data)

    def perform_destroy(self, instance):
        run_service(delete_draft_bill, instance, actor=self.request.user)


class BillAction(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = BillList.required_permissions

    def respond(self, pk, service, **kwargs):
        run_service(service, get_object_or_404(Bill, pk=pk), actor=self.request.user, **kwargs)
        return Response(BillDetailSerializer(BILLS.get(pk=pk)).data)


class BillApprove(BillAction):
    write_permissions = {"POST": ["accounting.approve_bill"]}

    def post(self, request, pk):
        return self.respond(pk, approve_bill)


class BillPost(BillAction):
    write_permissions = {"POST": ["accounting.post_bill"]}

    def post(self, request, pk):
        return self.respond(pk, post_bill)


class BillCancel(BillAction):
    write_permissions = {"POST": ["accounting.cancel_bill"]}

    def post(self, request, pk):
        return self.respond(pk, cancel_bill, **validated(CancellationInput, request.data))


SUPPLIER_PAYMENTS = SupplierPayment.objects.with_balances().prefetch_related("allocations").order_by(
    "-paid_on", "id")


class SupplierPaymentList(generics.ListCreateAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_supplierpayment", "accounting.view_supplierpaymentallocation"]
    write_permissions = {"POST": ["accounting.add_supplierpayment", "accounting.add_supplierpaymentallocation"]}
    serializer_class = SupplierPaymentSerializer
    queryset = SUPPLIER_PAYMENTS
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["supplier"]

    def create(self, request, *args, **kwargs):
        data = validated(SupplierPaymentInput, request.data)
        payment, code = replay_aware(SupplierPayment, {"reference": data["reference"]}, record_supplier_payment,
                                     data.pop("supplier"), actor=request.user, **data)
        return Response(SupplierPaymentSerializer(SUPPLIER_PAYMENTS.get(pk=payment.pk)).data, status=code)


class SupplierPaymentDetail(generics.RetrieveAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = SupplierPaymentList.required_permissions
    serializer_class = SupplierPaymentSerializer
    queryset = SUPPLIER_PAYMENTS


class SupplierAllocationCreate(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = SupplierPaymentList.required_permissions
    write_permissions = {"POST": ["accounting.add_supplierpaymentallocation"]}

    def post(self, request, pk):
        payment = get_object_or_404(SupplierPayment, pk=pk)
        data = validated(LaterSupplierAllocationInput, request.data)
        allocation, code = replay_aware(SupplierPaymentAllocation, {"reference": data["reference"]},
                                        allocate_supplier_payment, payment, actor=request.user, **data)
        return Response(SupplierAllocationSerializer(allocation).data, status=code)


class APAgingReport(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = BillList.required_permissions + ["accounting.view_supplierpayment"]

    def get(self, request):
        query = AgingQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        as_of = query.validated_data["as_of"]
        return Response({"as_of": as_of.isoformat(), "currencies": report_json(bill_aging(as_of=as_of))})


class SupplierStatement(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_supplier", "accounting.view_bill", "accounting.view_supplierpayment"]

    def get(self, request, pk):
        supplier = get_object_or_404(Supplier, pk=pk)
        return Response(report_json(run_service(supplier_statement, supplier, **period(request))))
