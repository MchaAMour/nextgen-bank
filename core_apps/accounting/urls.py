from django.urls import path

from .ap_views import (APAgingReport, BillApprove, BillCancel, BillDetail, BillList, BillPost,
    SupplierAllocationCreate, SupplierDetail, SupplierList, SupplierPaymentDetail, SupplierPaymentList,
    SupplierStatement)
from .ar_views import (AgingReport, AllocationReleaseCreate, CreditNoteList, CustomerDetail, CustomerList,
    InvoiceCancel, InvoiceCheckout, InvoiceDetail, InvoiceList, InvoicePost, PaymentAllocationCreate, PaymentDetail,
    PaymentList, PaymentRefundCreate)
from .bank_views import (AutoMatch, BankReconciliationReport, LineMatch, LinePost, StatementCsvImport,
    StatementDetail, StatementList)
from .report_views import AuditEventList, BalanceSheet, CustomerStatement, PeriodCloseList, ProfitAndLoss
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
    path("allocations/<uuid:pk>/release/", AllocationReleaseCreate.as_view(), name="allocation-release"),
    path("payments/<uuid:pk>/refunds/", PaymentRefundCreate.as_view(), name="payment-refunds"),
    path("credit-notes/", CreditNoteList.as_view(), name="credit-notes"),
    path("ar-aging/", AgingReport.as_view(), name="ar-aging"),
    path("suppliers/", SupplierList.as_view(), name="suppliers"),
    path("suppliers/<uuid:pk>/", SupplierDetail.as_view(), name="supplier-detail"),
    path("suppliers/<uuid:pk>/statement/", SupplierStatement.as_view(), name="supplier-statement"),
    path("bills/", BillList.as_view(), name="bills"),
    path("bills/<uuid:pk>/", BillDetail.as_view(), name="bill-detail"),
    path("bills/<uuid:pk>/approve/", BillApprove.as_view(), name="bill-approve"),
    path("bills/<uuid:pk>/post/", BillPost.as_view(), name="bill-post"),
    path("bills/<uuid:pk>/cancel/", BillCancel.as_view(), name="bill-cancel"),
    path("supplier-payments/", SupplierPaymentList.as_view(), name="supplier-payments"),
    path("supplier-payments/<uuid:pk>/", SupplierPaymentDetail.as_view(), name="supplier-payment-detail"),
    path("supplier-payments/<uuid:pk>/allocations/", SupplierAllocationCreate.as_view(),
         name="supplier-payment-allocations"),
    path("ap-aging/", APAgingReport.as_view(), name="ap-aging"),
    path("bank-statements/", StatementList.as_view(), name="bank-statements"),
    path("bank-statements/import-csv/", StatementCsvImport.as_view(), name="bank-statement-csv"),
    path("bank-statements/auto-match/", AutoMatch.as_view(), name="bank-auto-match"),
    path("bank-statements/<uuid:pk>/", StatementDetail.as_view(), name="bank-statement-detail"),
    path("bank-lines/<uuid:pk>/match/", LineMatch.as_view(), name="bank-line-match"),
    path("bank-lines/<uuid:pk>/post/", LinePost.as_view(), name="bank-line-post"),
    path("bank-reconciliation/", BankReconciliationReport.as_view(), name="bank-reconciliation"),
    path("customers/<uuid:pk>/statement/", CustomerStatement.as_view(), name="customer-statement"),
    path("balance-sheet/", BalanceSheet.as_view(), name="balance-sheet"),
    path("profit-and-loss/", ProfitAndLoss.as_view(), name="profit-and-loss"),
    path("audit-events/", AuditEventList.as_view(), name="audit-events"),
    path("periods/", PeriodCloseList.as_view(), name="periods"),
    path("accounts/", AccountList.as_view(), name="accounts"),
    path("entries/", EntryList.as_view(), name="entries"),
    path("entries/<uuid:pk>/", EntryDetail.as_view(), name="entry-detail"),
    path("general-ledger/", GeneralLedger.as_view(), name="general-ledger"),
    path("trial-balance/", TrialBalance.as_view(), name="trial-balance"),
]
