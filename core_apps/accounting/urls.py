from django.urls import path

from .ar_views import (AgingReport, CreditNoteList, CustomerDetail, CustomerList, InvoiceCancel,
    InvoiceDetail, InvoiceList, InvoicePost, PaymentAllocationCreate, PaymentDetail, PaymentList,
    PaymentRefundCreate)
from .views import AccountList, EntryList, EntryDetail, GeneralLedger, TrialBalance

app_name = "accounting"

urlpatterns = [
    path("customers/", CustomerList.as_view(), name="customers"),
    path("customers/<uuid:pk>/", CustomerDetail.as_view(), name="customer-detail"),
    path("invoices/", InvoiceList.as_view(), name="invoices"),
    path("invoices/<uuid:pk>/", InvoiceDetail.as_view(), name="invoice-detail"),
    path("invoices/<uuid:pk>/post/", InvoicePost.as_view(), name="invoice-post"),
    path("invoices/<uuid:pk>/cancel/", InvoiceCancel.as_view(), name="invoice-cancel"),
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
