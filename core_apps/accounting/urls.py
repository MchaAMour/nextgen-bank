from django.urls import path

from .ar_views import CustomerList, InvoiceList, ReceiptList, AgingReport
from .views import AccountList, EntryList, EntryDetail, GeneralLedger, TrialBalance

app_name = "accounting"

urlpatterns = [
    path("customers/", CustomerList.as_view(), name="customers"),
    path("invoices/", InvoiceList.as_view(), name="invoices"),
    path("receipts/", ReceiptList.as_view(), name="receipts"),
    path("ar-aging/", AgingReport.as_view(), name="ar-aging"),
    path("accounts/", AccountList.as_view(), name="accounts"),
    path("entries/", EntryList.as_view(), name="entries"),
    path("entries/<uuid:pk>/", EntryDetail.as_view(), name="entry-detail"),
    path("general-ledger/", GeneralLedger.as_view(), name="general-ledger"),
    path("trial-balance/", TrialBalance.as_view(), name="trial-balance"),
]
