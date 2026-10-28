from rest_framework import serializers

from .models import ChartOfAccount, JournalEntry, JournalLine


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChartOfAccount
        fields = ["id", "code", "name", "account_type", "normal_balance", "is_active"]


class LineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    entry_date = serializers.DateField(source="entry.entry_date", read_only=True)
    reference = serializers.CharField(source="entry.reference", read_only=True)

    class Meta:
        model = JournalLine
        fields = [
            "id",
            "entry",
            "entry_date",
            "reference",
            "account",
            "account_code",
            "description",
            "debit",
            "credit",
            "currency",
        ]


class EntrySerializer(serializers.ModelSerializer):
    lines = LineSerializer(many=True, read_only=True)

    class Meta:
        model = JournalEntry
        fields = [
            "id",
            "entry_date",
            "memo",
            "reference",
            "status",
            "posted_at",
            "posted_by",
            "reversal_of",
            "lines",
        ]


class ReportFilterSerializer(serializers.Serializer):
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    currency = serializers.RegexField(r"^[A-Z]{3}$", required=False)
    account = serializers.UUIDField(required=False)

    def validate(self, attrs):
        if (
            attrs.get("start_date")
            and attrs.get("end_date")
            and attrs["start_date"] > attrs["end_date"]
        ):
            raise serializers.ValidationError(
                "start_date must be on or before end_date."
            )
        return attrs
