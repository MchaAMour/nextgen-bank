"""Pages Stripe Checkout returns customers to. They show status only.

The webhook records payments; these pages never fulfil anything, so a customer
who closes the tab or forges a URL changes nothing. They are public: a Checkout
Session ID or invoice ID, both unguessable, identifies what to show.
"""

from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import StripeCheckoutSession

Status = StripeCheckoutSession.Status
# Seconds between reloads while the webhook has not arrived.
REFRESH_SECONDS = 3
MESSAGES = {
    Status.PAID: ("paid", "Payment received", "Thank you. Your payment has been received and applied to the invoice."),
    Status.AWAITING_PAYMENT: ("processing", "Payment processing",
                              "Your bank is still confirming the payment. We will apply it once it clears."),
    Status.OPEN: ("confirming", "Confirming your payment", "This page updates in a moment."),
    Status.FAILED: ("failed", "Payment failed", "The payment did not go through. No money was taken."),
    Status.EXPIRED: ("failed", "Payment link expired", "This payment link has expired. Ask us for a new one."),
}


def _page(request, state, title, message, session=None, status=200, refresh=False, resume_url=None):
    return render(request, "accounting/payment_status.html", {
        "state": state, "title": title, "message": message, "session": session, "refresh": refresh,
        "refresh_seconds": REFRESH_SECONDS, "resume_url": resume_url}, status=status)


def _not_found(request):
    return _page(request, "failed", "Payment not found", "We could not find this payment. Check the link you "
                 "followed, or contact us.", status=404)


@never_cache
@require_GET
def payment_success(request):
    session_id = request.GET.get("session_id", "")
    session = StripeCheckoutSession.objects.select_related("invoice").filter(stripe_session_id=session_id).first()
    if not session_id.startswith("cs_") or session is None:
        return _not_found(request)
    state, title, message = MESSAGES[session.status]
    return _page(request, state, title, message, session, refresh=session.status == Status.OPEN)


@never_cache
@require_GET
def payment_cancelled(request):
    try:
        sessions = StripeCheckoutSession.objects.select_related("invoice").filter(
            invoice_id=request.GET.get("invoice", ""))
        session = sessions.order_by("-created_at").first()
    except ValidationError:  # Not a UUID.
        session = None
    resumable = (session is not None and session.status == Status.OPEN and session.expires_at > timezone.now())
    if session is not None and session.status != Status.OPEN:
        state, title, message = MESSAGES[session.status]
        return _page(request, state, title, message, session)
    message = ("No payment was taken. You can return to the payment page while the link is valid."
               if resumable else "No payment was taken.")
    return _page(request, "cancelled", "Payment cancelled", message, session,
                 resume_url=session.url if resumable else None)
