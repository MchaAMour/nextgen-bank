from django.conf import settings
from django.core.checks import Error, register


@register()
def base_currency_matches_ledger(app_configs, **kwargs):
    from .models import BASE_CURRENCY

    if settings.ACCOUNTING_BASE_CURRENCY != BASE_CURRENCY:
        return [Error(
            f"ACCOUNTING_BASE_CURRENCY is {settings.ACCOUNTING_BASE_CURRENCY!r}, but the ledger only supports "
            f"{BASE_CURRENCY!r}.",
            hint="Remove the override or set it to the ledger currency; other currencies need FX accounting.",
            id="accounting.E001",
        )]
    return []
