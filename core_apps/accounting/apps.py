from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core_apps.accounting"
    verbose_name = _("Accounting")
