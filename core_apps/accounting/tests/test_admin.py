import pytest
from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from .test_accounts_payable import approver, clerk, posted, supplier
from .test_ar_workflow import invoice
from .test_bank_reconciliation import JANUARY, books, statement

pytestmark = pytest.mark.django_db
# Document lines are shown inline on their invoice or bill only.
INLINE_ONLY = {"invoiceline", "billline"}
MODELS = [model for model in apps.get_app_config("accounting").get_models()
          if model._meta.model_name not in INLINE_ONLY]


def staff(email, superuser=False):
    user = get_user_model().objects.create(
        email=email, username=email.split("@")[0][:12], id_no=30000 + len(email), first_name="Admin",
        last_name="Viewer", security_question="favorite_color", security_answer="blue", is_staff=True,
        is_superuser=superuser)
    client = Client()
    client.force_login(user)
    return user, client


def url(model, view, *args):
    return reverse(f"admin:accounting_{model._meta.model_name}_{view}", args=args)


@pytest.fixture
def books_with_everything(books, posted):
    """Ledger entries, AR, AP, a bank statement with matches, a credit note and audit events."""
    statement(lines=JANUARY)


def test_every_model_is_registered_read_only(books_with_everything):
    _, client = staff("root@example.com", superuser=True)
    assert not any(apps.get_model("accounting", name) in admin.site._registry for name in INLINE_ONLY)
    for model in MODELS:
        assert model in admin.site._registry, model.__name__
        assert client.get(url(model, "changelist")).status_code == 200, model.__name__
        assert client.get(url(model, "add")).status_code == 403, model.__name__
        obj = model.objects.first()
        if obj is None:
            continue
        change = url(model, "change", obj.pk)
        page = client.get(change)
        assert page.status_code == 200 and b'name="_save"' not in page.content, model.__name__
        assert client.post(change, {}).status_code == 403, model.__name__
        assert client.get(url(model, "delete", obj.pk)).status_code == 403, model.__name__
        assert model.objects.filter(pk=obj.pk).exists()


def test_changelists_offer_no_bulk_actions(books_with_everything):
    _, client = staff("root@example.com", superuser=True)
    page = client.get(url(apps.get_model("accounting", "Invoice"), "changelist"))
    assert b'name="action"' not in page.content


def test_viewing_needs_the_model_view_permission(books_with_everything):
    user, client = staff("clerk@example.com")
    invoices = url(apps.get_model("accounting", "Invoice"), "changelist")
    assert client.get(invoices).status_code == 403
    user.user_permissions.add(Permission.objects.get(codename="view_invoice"))
    assert client.get(invoices).status_code == 200
