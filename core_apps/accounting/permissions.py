from rest_framework.permissions import SAFE_METHODS, BasePermission


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



class AccountingPermission(BasePermission):
    """Read permissions for safe methods; each write method names its own permissions."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        if request.method in SAFE_METHODS:
            return user.has_perms(view.required_permissions)
        method = request.method.lower()
        if method not in view.http_method_names or not hasattr(view, method):
            return True  # The view answers 405 Method Not Allowed.
        required = getattr(view, "write_permissions", {}).get(request.method)
        return bool(required) and user.has_perms(required)
