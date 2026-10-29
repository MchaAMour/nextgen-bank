from django.shortcuts import get_object_or_404
from django_filters import rest_framework as filters
from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .ar_views import AgingQuery, run_service
from .financial_reports import balance_sheet, customer_statement, profit_and_loss
from .models import AuditEvent, Customer, PeriodClose
from .period_close import close_period
from .permissions import AccountingPermission
from .views import LedgerPagination, report_json

LEDGER_READ = ["accounting.view_journalentry", "accounting.view_journalline", "accounting.view_chartofaccount"]


class PeriodQuery(serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()


def period(request):
    query = PeriodQuery(data=request.query_params)
    query.is_valid(raise_exception=True)
    return query.validated_data


class BalanceSheet(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = LEDGER_READ

    def get(self, request):
        query = AgingQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(report_json(balance_sheet(as_of=query.validated_data["as_of"])))


class ProfitAndLoss(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = LEDGER_READ

    def get(self, request):
        return Response(report_json(run_service(profit_and_loss, **period(request))))


class CustomerStatement(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_customer", "accounting.view_invoice", "accounting.view_customerpayment",
                            "accounting.view_paymentrefund", "accounting.view_creditnote"]

    def get(self, request, pk):
        customer = get_object_or_404(Customer, pk=pk)
        return Response(report_json(run_service(customer_statement, customer, **period(request))))


class AuditEventFilter(filters.FilterSet):
    since = filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    until = filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        model = AuditEvent
        fields = ["action", "object_type", "object_id", "actor"]


class AuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = AuditEvent
        fields = ["id", "created_at", "action", "object_type", "object_id", "summary", "details", "actor",
                  "actor_email"]


class AuditEventList(generics.ListAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_auditevent"]
    serializer_class = AuditEventSerializer
    queryset = AuditEvent.objects.select_related("actor").order_by("-created_at", "id")
    pagination_class = LedgerPagination
    filter_backends = [filters.DjangoFilterBackend]
    filterset_class = AuditEventFilter


class PeriodCloseSerializer(serializers.ModelSerializer):
    class Meta:
        model = PeriodClose
        fields = ["id", "closed_through", "closing_entry", "closed_by", "created_at"]


class PeriodCloseInput(serializers.Serializer):
    through = serializers.DateField()
    transfer_earnings = serializers.BooleanField(default=False)


class PeriodCloseList(generics.ListAPIView):
    """GET lists closes, newest first; POST closes the books through a date."""
    permission_classes = [AccountingPermission]
    required_permissions = ["accounting.view_periodclose"]
    write_permissions = {"POST": ["accounting.add_periodclose", "accounting.add_journalentry"]}
    serializer_class = PeriodCloseSerializer
    queryset = PeriodClose.objects.order_by("-closed_through")
    pagination_class = LedgerPagination

    def post(self, request):
        query = PeriodCloseInput(data=request.data)
        query.is_valid(raise_exception=True)
        close = run_service(close_period, actor=request.user, **query.validated_data)
        return Response(PeriodCloseSerializer(close).data, status=status.HTTP_201_CREATED)
