from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers, status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .ar_views import AgingQuery, DecimalString, replay_aware, run_service, validated
from .bank_reconciliation import (auto_match, bank_reconciliation, import_bank_statement, match_line,
    parse_statement_csv, post_statement_line, unmatch_line)
from .models import BankStatement, BankStatementLine, JournalLine
from .permissions import AccountingPermission
from .views import LedgerPagination

READ = ["accounting.view_bankstatement", "accounting.view_bankstatementline", "accounting.view_bankmatch"]
IMPORT = ["accounting.add_bankstatement", "accounting.add_bankstatementline", "accounting.add_bankmatch"]


class StatementLineInput(serializers.Serializer):
    booked_on = serializers.DateField()
    amount = DecimalString(max_digits=18, decimal_places=2)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    bank_reference = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class StatementHeaderInput(serializers.Serializer):
    reference = serializers.CharField(max_length=100)
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    opening_balance = DecimalString(max_digits=18, decimal_places=2)
    closing_balance = DecimalString(max_digits=18, decimal_places=2)


class StatementInput(StatementHeaderInput):
    lines = StatementLineInput(many=True)


class CsvStatementInput(StatementHeaderInput):
    file = serializers.FileField()


class MatchSerializer(serializers.Serializer):
    journal_line = serializers.UUIDField(source="journal_line_id")
    entry = serializers.UUIDField(source="journal_line.entry_id")
    method = serializers.CharField()
    matched_by = serializers.PrimaryKeyRelatedField(read_only=True)
    created_at = serializers.DateTimeField()


class StatementLineSerializer(serializers.ModelSerializer):
    match = serializers.SerializerMethodField()

    class Meta:
        model = BankStatementLine
        fields = ["id", "sequence", "booked_on", "amount", "description", "bank_reference", "match"]

    def get_match(self, line):
        match = getattr(line, "match", None)
        return MatchSerializer(match).data if match else None


class StatementSerializer(serializers.ModelSerializer):
    account = serializers.CharField(source="account.code")
    lines = StatementLineSerializer(many=True)

    class Meta:
        model = BankStatement
        fields = ["id", "account", "reference", "start_date", "end_date", "opening_balance", "closing_balance",
                  "currency", "imported_by", "created_at", "lines"]


STATEMENTS = BankStatement.objects.select_related("account").prefetch_related(
    "lines__match__journal_line").order_by("-end_date", "id")


def imported(request, data):
    statement, code = replay_aware(BankStatement, {"reference": data["reference"]}, import_bank_statement,
                                   actor=request.user, **data)
    return Response(StatementSerializer(STATEMENTS.get(pk=statement.pk)).data, status=code)


class StatementList(generics.ListAPIView):
    """List statements, or import one as JSON; its lines are matched automatically."""
    permission_classes = [AccountingPermission]
    required_permissions = READ
    write_permissions = {"POST": IMPORT}
    serializer_class = StatementSerializer
    queryset = STATEMENTS
    pagination_class = LedgerPagination

    def post(self, request):
        return imported(request, validated(StatementInput, request.data))


class StatementCsvImport(APIView):
    """Multipart: the statement header fields and a CSV `file` with date, amount, description, reference."""
    permission_classes = [AccountingPermission]
    write_permissions = {"POST": IMPORT}
    parser_classes = [MultiPartParser]

    def post(self, request):
        data = validated(CsvStatementInput, request.data)
        upload = data.pop("file")
        try:
            text = upload.read().decode("utf-8")
        except UnicodeDecodeError:
            raise serializers.ValidationError({"file": ["The CSV must be UTF-8."]})
        data["lines"] = run_service(parse_statement_csv, text)
        return imported(request, data)


class StatementDetail(generics.RetrieveAPIView):
    permission_classes = [AccountingPermission]
    required_permissions = READ
    serializer_class = StatementSerializer
    queryset = STATEMENTS


class AutoMatch(APIView):
    permission_classes = [AccountingPermission]
    write_permissions = {"POST": ["accounting.add_bankmatch"]}

    def post(self, request):
        return Response(run_service(auto_match, actor=request.user))


class MatchInput(serializers.Serializer):
    journal_line = serializers.PrimaryKeyRelatedField(queryset=JournalLine.objects.all())


class PostLineInput(serializers.Serializer):
    account = serializers.CharField(max_length=20)
    memo = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class LineAction(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = READ

    def line(self, pk):
        return get_object_or_404(BankStatementLine, pk=pk)

    def respond(self, line, code=status.HTTP_200_OK):
        line = BankStatementLine.objects.select_related("match__journal_line").get(pk=line.pk)
        return Response(StatementLineSerializer(line).data, status=code)


class LineMatch(LineAction):
    """POST matches the line with a ledger line; DELETE unmatches it."""
    write_permissions = {"POST": ["accounting.add_bankmatch"], "DELETE": ["accounting.delete_bankmatch"]}

    def post(self, request, pk):
        line = self.line(pk)
        data = validated(MatchInput, request.data)
        run_service(match_line, line, journal_line=data["journal_line"], actor=request.user)
        return self.respond(line, status.HTTP_201_CREATED)

    def delete(self, request, pk):
        line = self.line(pk)
        run_service(unmatch_line, line)
        return self.respond(line)


class LinePost(LineAction):
    """Post a bank-only item (charges, interest) to another account and match it."""
    write_permissions = {"POST": ["accounting.add_bankmatch", "accounting.add_journalentry"]}

    def post(self, request, pk):
        line = self.line(pk)
        data = validated(PostLineInput, request.data)
        run_service(post_statement_line, line, account_code=data["account"], memo=data["memo"],
                    actor=request.user)
        return self.respond(line, status.HTTP_201_CREATED)


def _json(value):
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


class BankReconciliationReport(APIView):
    permission_classes = [AccountingPermission]
    required_permissions = READ + ["accounting.view_journalline"]

    def get(self, request):
        query = AgingQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        report = run_service(bank_reconciliation, as_of=query.validated_data["as_of"])
        return Response(_json(report))
