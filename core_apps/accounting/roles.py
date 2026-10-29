"""Finance roles as Django Groups, each with only the accounting permissions its work needs (AUTH-01).

`python manage.py setup_accounting_roles` creates or updates them; running it again
resets each group to exactly these permissions. Assign users to groups in the admin.
"""

from django.contrib.auth.models import Group, Permission
from django.db import transaction

VIEW_ALL = "view_*"
ROLES = {
    "Accounting viewer": [VIEW_ALL],
    "AR clerk": [VIEW_ALL, "add_customer", "change_customer", "add_invoice", "change_invoice", "delete_invoice",
                 "add_customerpayment", "add_paymentallocation", "add_stripecheckoutsession"],
    "AR manager": [VIEW_ALL, "post_invoice", "cancel_invoice", "add_paymentrefund", "add_creditnote"],
    "AP clerk": [VIEW_ALL, "add_supplier", "change_supplier", "add_bill", "change_bill", "delete_bill",
                 "add_supplierpayment", "add_supplierpaymentallocation"],
    "AP approver": [VIEW_ALL, "approve_bill", "post_bill", "cancel_bill"],
    "Reconciler": [VIEW_ALL, "add_bankstatement", "add_bankstatementline", "add_bankmatch", "delete_bankmatch",
                   "add_journalentry", "add_stripebalancetransaction"],
}
# Only controllers close periods.
ROLES["Controller"] = sorted({codename for codenames in ROLES.values() for codename in codenames} | {"add_periodclose"})


def _permissions(codenames):
    accounting = Permission.objects.filter(content_type__app_label="accounting")
    wanted = {codename for codename in codenames if codename != VIEW_ALL}
    found = set(accounting.filter(codename__in=wanted).values_list("codename", flat=True))
    missing = wanted - found
    if missing:
        raise LookupError(f"Unknown accounting permissions: {', '.join(sorted(missing))}. Run migrations first.")
    chosen = accounting.filter(codename__in=wanted)
    if VIEW_ALL in codenames:
        chosen = chosen | accounting.filter(codename__startswith="view_")
    return chosen


@transaction.atomic
def setup_roles():
    """Create or reset every role; returns {group name: permission count}."""
    counts = {}
    for name, codenames in ROLES.items():
        group, _ = Group.objects.get_or_create(name=name)
        permissions = list(_permissions(codenames))
        group.permissions.set(permissions)
        counts[name] = len(permissions)
    return counts
