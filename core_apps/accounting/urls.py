from django.urls import path

from .ar_views import (AgingReport, CreditNoteList, CustomerDetail, CustomerList, InvoiceCancel,
    InvoiceCheckout, InvoiceDetail, InvoiceList, InvoicePost, PaymentAllocationCreate, PaymentDetail,
    PaymentList, PaymentRefundCreate)
from .stripe_views import BalanceTransactionList, BalanceTransactionSync, StripeReconciliation, StripeWebhook
from .views import AccountList, EntryList, EntryDetail, GeneralLedger, TrialBalance

app_name = "accounting"

urlpatterns = [
    path("customers/", CustomerList.as_view(), name="customers"),
    path("customers/<uuid:pk>/", CustomerDetail.as_view(), name="customer-detail"),
    path("invoices/", InvoiceList.as_view(), name="invoices"),
    path("invoices/<uuid:pk>/", InvoiceDetail.as_view(), name="invoice-detail"),
    path("invoices/<uuid:pk>/post/", InvoicePost.as_view(), name="invoice-post"),
    path("invoices/<uuid:pk>/cancel/", InvoiceCancel.as_view(), name="invoice-cancel"),
    path("invoices/<uuid:pk>/checkout/", InvoiceCheckout.as_view(), name="invoice-checkout"),
    path("stripe/webhook/", StripeWebhook.as_view(), name="stripe-webhook"),
    path("stripe/balance-transactions/", BalanceTransactionList.as_view(), name="stripe-balance-transactions"),
    path("stripe/balance-transactions/sync/", BalanceTransactionSync.as_view(), name="stripe-balance-sync"),
    path("stripe/reconciliation/", StripeReconciliation.as_view(), name="stripe-reconciliation"),
    path("payments/", PaymentList.as_view(), name="payments"),
    path("payments/<uuid:pk>/", PaymentDetail.as_view(), name="payment-detail"),
    path("payments/<uuid:pk>/allocations/", PaymentAllocationCreate.as_view(), name="payment-allocations"),
    path("payments/<uuid:pk>/refunds/", PaymentRefundCreate.as_view(), name="payment-refunds"),
    path("credit-notes/", CreditNoteList.as_view(), name="credit-notes"),
    path("ar-aging/", AgingReport.as_view(), name="ar-aging"),
    path("accounts/", AccountList.as_view(), name="accounts"),
    path("entries/", EntryList.as_view(), name="entries"),
    path("entries/<uuid:pk>/", EntryDetail.as_view(), name="entry-detail"),
    path("general-ledger/", GeneralLedger.as_view(), name="general-ledger"),
    path("trial-balance/", TrialBalance.as_view(), name="trial-balance"),
]
