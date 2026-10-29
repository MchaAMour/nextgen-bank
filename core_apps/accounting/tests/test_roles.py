import io

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from rest_framework.test import APIClient

from core_apps.accounting.models import Customer
from core_apps.accounting.roles import ROLES, setup_roles
from .test_accounts_payable import make_user

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"


def perms(name):
    return set(Group.objects.get(name=name).permissions.values_list("codename", flat=True))


def test_setup_is_idempotent_and_resets_extra_permissions():
    first = setup_roles()
    clerk = Group.objects.get(name="AR clerk")
    clerk.permissions.add(*Group.objects.get(name="AP approver").permissions.all())
    assert "approve_bill" in perms("AR clerk")
    assert setup_roles() == first
    assert "approve_bill" not in perms("AR clerk")
    assert Group.objects.filter(name__in=ROLES).count() == len(ROLES)
    call_command("setup_accounting_roles", stdout=io.StringIO())
    assert setup_roles() == first


def test_roles_are_least_privilege():
    setup_roles()
    viewer = perms("Accounting viewer")
    assert viewer and all(codename.startswith("view_") for codename in viewer)
    assert {"post_invoice", "approve_bill"}.isdisjoint(perms("AR clerk"))
    assert {"approve_bill", "post_bill", "add_bill"}.isdisjoint(perms("AR manager"))
    assert {"approve_bill", "post_bill", "add_customerpayment"}.isdisjoint(perms("AP clerk"))
    assert {"add_bill", "add_supplierpayment"}.isdisjoint(perms("AP approver"))
    for role in ROLES:
        assert not any(codename.startswith(("change_journal", "delete_journal", "change_audit", "delete_audit"))
                       for codename in perms(role)), role
    assert perms("Controller") == set().union(*(perms(role) for role in ROLES if role != "Controller"))


def as_member(role, number):
    user = make_user(number)
    user.groups.add(Group.objects.get(name=role))
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_roles_through_the_api():
    setup_roles()
    viewer, ar_clerk, ar_manager = (as_member(role, n) for n, role in
                                    enumerate(["Accounting viewer", "AR clerk", "AR manager"], start=1))
    assert viewer.get(BASE + "invoices/").status_code == 200
    assert viewer.post(BASE + "customers/", {"name": "No"}, format="json").status_code == 403
    customer = ar_clerk.post(BASE + "customers/", {"name": "Role customer"}, format="json")
    assert customer.status_code == 201
    draft = ar_clerk.post(BASE + "invoices/", {
        "customer": customer.data["id"], "number": "ROLE-1", "issue_date": "2026-01-01", "due_date": "2026-01-31",
        "lines": [{"description": "Work", "quantity": "1", "unit_price": "10.00"}]}, format="json")
    assert draft.status_code == 201
    assert ar_clerk.post(f"{BASE}invoices/{draft.data['id']}/post/").status_code == 403
    assert ar_manager.post(f"{BASE}invoices/{draft.data['id']}/post/").status_code == 200
    assert Customer.objects.count() == 1
