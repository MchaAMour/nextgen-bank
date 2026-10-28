from decimal import Decimal

from django.db.models import Sum
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ChartOfAccount, JournalEntry, JournalLine
from .permissions import LedgerReadPermission
from .serializers import (
    AccountSerializer,
    EntrySerializer,
    LineSerializer,
    ReportFilterSerializer,
)


class LedgerPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class AccountList(generics.ListAPIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = ["accounting.view_chartofaccount"]
    queryset = ChartOfAccount.objects.order_by("code", "id")
    serializer_class = AccountSerializer
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["account_type", "is_active"]
    search_fields = ["code", "name"]


class EntryList(generics.ListAPIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = [
        "accounting.view_journalentry",
        "accounting.view_journalline",
    ]
    queryset = JournalEntry.objects.prefetch_related(
        "lines__account", "lines__entry"
    ).order_by("-entry_date", "-created_at", "id")
    serializer_class = EntrySerializer
    pagination_class = LedgerPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "entry_date", "reference"]


class EntryDetail(generics.RetrieveAPIView):
    permission_classes = EntryList.permission_classes
    required_permissions = EntryList.required_permissions
    queryset = EntryList.queryset
    serializer_class = EntrySerializer


def report_lines(params):
    """Date filters use the accounting date, including both endpoints."""
    serializer = ReportFilterSerializer(data=params)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    # Reversals remain part of ledger history; their correcting entries offset them.
    lines = JournalLine.objects.filter(
        entry__status__in=[JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED]
    )
    for key, lookup in {
        "start_date": "entry__entry_date__gte",
        "end_date": "entry__entry_date__lte",
        "currency": "currency",
        "account": "account_id",
    }.items():
        if key in data:
            lines = lines.filter(**{lookup: data[key]})
    return lines


class GeneralLedger(generics.ListAPIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = EntryList.required_permissions
    serializer_class = LineSerializer
    pagination_class = LedgerPagination

    def get_queryset(self):
        return (
            report_lines(self.request.query_params)
            .select_related("entry", "account")
            .order_by(
                "entry__entry_date", "entry__created_at", "entry_id", "created_at", "id"
            )
        )


def money(amount):
    return format(amount, ".2f")


class TrialBalance(APIView):
    permission_classes = [LedgerReadPermission]
    required_permissions = EntryList.required_permissions

    def get(self, request):
        totals = (
            report_lines(request.query_params)
            .order_by()
            .values("currency", "account_id", "account__code", "account__name")
            .annotate(debits=Sum("debit"), credits=Sum("credit"))
            .order_by("currency", "account__code")
        )
        currencies = {}
        for row in totals:
            group = currencies.setdefault(
                row["currency"],
                {
                    "currency": row["currency"],
                    "accounts": [],
                    "debit_total": Decimal("0"),
                    "credit_total": Decimal("0"),
                },
            )
            net = row["debits"] - row["credits"]
            debit, credit = max(net, Decimal("0")), max(-net, Decimal("0"))
            group["accounts"].append(
                {
                    "account": str(row["account_id"]),
                    "code": row["account__code"],
                    "name": row["account__name"],
                    "debit": money(debit),
                    "credit": money(credit),
                }
            )
            group["debit_total"] += debit
            group["credit_total"] += credit
        for group in currencies.values():
            group["balanced"] = group["debit_total"] == group["credit_total"]
            group["debit_total"] = money(group["debit_total"])
            group["credit_total"] = money(group["credit_total"])
        return Response({"currencies": list(currencies.values())})
