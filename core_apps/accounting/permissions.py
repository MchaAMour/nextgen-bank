from rest_framework.permissions import BasePermission


class LedgerReadPermission(BasePermission):
    """Explicit model view permissions, assignable through Django Groups."""

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.has_perms(view.required_permissions)
        )
