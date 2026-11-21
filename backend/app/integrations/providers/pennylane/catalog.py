"""The rest of the Company API v2 surface, one curated tool per endpoint.

The domain modules hand-render the core reads and guard the core writes;
this table covers every other endpoint so the provider exposes the whole
API: bank accounts and establishments, mandates (SEPA, GoCardless, Pro
account), lettering, journals, ledger accounts, categories, products,
quotes, commercial documents, billing subscriptions, purchase requests,
customer contacts, invoice lifecycle (finalize, send, import, credit note,
appendices, e-invoice status), transactions, webhook subscriptions,
numberings and templates.

Each tool is a read when its method is GET, else a write (the method decides,
so integration_read_only withholds every write). Writes that move money,
change bank data, sign or cancel a mandate, send a document outside the
company or delete a record declare ``always_ask``: each call waits for a
person's approval. Bank and mandate tools also form
:data:`BANK_DETAIL_TOOLS`, one group a binding can withhold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import CURSOR, LIMIT, PennylaneContext, clamp, definition, line, page_footer, text


@dataclass(frozen=True)
class Op:
    name: str
    method: str
    path: str
    description: str
    #: Body fields the tool takes (name: JSON schema); a write passes them through.
    body: tuple[tuple[str, dict[str, Any]], ...] = ()
    required: tuple[str, ...] = ()
    #: Query filters (name: schema) of a list read.
    query: tuple[tuple[str, dict[str, Any]], ...] = ()
    paged: bool = False
    always_ask: bool = False
    bank: bool = False
    #: A body that is a JSON list (categorisation): the tool takes it as ``items``.
    list_body: bool = False


S = {"type": "string"}
I = {"type": "integer"}  # noqa: E741
B = {"type": "boolean"}
O = {"type": "object"}  # noqa: E741
A = {"type": "array", "items": {}}
FILTER = (("filter", {"type": "string", "description": "Pennylane filter JSON: [{field, operator, value}]"}),)
WEIGHTS = {"type": "array", "items": O, "description": "[{id, weight}] (weights 0-1 as strings)"}

OPS: tuple[Op, ...] = (
    # --- Journals, ledger accounts, lettering --------------------------------
    Op("get_pennylane_journal", "get", "journals/{id}", "One journal."),
    Op(
        "create_pennylane_journal",
        "post",
        "journals",
        "Create a journal.",
        (("code", S), ("label", S)),
        ("code", "label"),
    ),
    Op("get_pennylane_ledger_account", "get", "ledger_accounts/{id}", "One ledger account."),
    Op(
        "create_pennylane_ledger_account",
        "post",
        "ledger_accounts",
        "Create a ledger account in the chart of accounts.",
        (("number", S), ("label", S), ("vat_rate", S), ("country_alpha2", S)),
        ("number", "label"),
    ),
    Op(
        "update_pennylane_ledger_account",
        "put",
        "ledger_accounts/{id}",
        "Update a ledger account.",
        (("label", S), ("vat_rate", S), ("enabled", B)),
    ),
    Op("get_pennylane_ledger_entry_line", "get", "ledger_entry_lines/{id}", "One ledger entry line."),
    Op(
        "list_pennylane_lettered_lines",
        "get",
        "ledger_entry_lines/{id}/lettered_ledger_entry_lines",
        "Lines lettered with a ledger entry line.",
        paged=True,
    ),
    Op(
        "get_pennylane_ledger_line_categories",
        "get",
        "ledger_entry_lines/{id}/categories",
        "Analytical categories of a ledger entry line.",
    ),
    Op(
        "letter_pennylane_ledger_lines",
        "post",
        "ledger_entry_lines/lettering",
        "Letter (reconcile) ledger entry lines together.",
        (
            ("ledger_entry_lines", {"type": "array", "items": O, "description": "[{id}]"}),
            ("unbalanced_lettering_strategy", S),
        ),
        ("ledger_entry_lines",),
    ),
    Op(
        "unletter_pennylane_ledger_lines",
        "delete",
        "ledger_entry_lines/lettering",
        "Remove the lettering of ledger entry lines.",
        (
            ("ledger_entry_lines", {"type": "array", "items": O, "description": "[{id}]"}),
            ("unbalanced_lettering_strategy", S),
        ),
        ("ledger_entry_lines",),
    ),
    Op(
        "list_pennylane_ledger_entry_files",
        "get",
        "ledger_entries/{id}/dms_files",
        "Documents attached to a ledger entry.",
        paged=True,
    ),
    Op(
        "attach_pennylane_ledger_file",
        "post",
        "ledger_attachments",
        "Attach an uploaded file to the ledger.",
        (("file_attachment_id", I), ("ledger_entry_id", I)),
    ),
    # --- Categories ------------------------------------------------------------
    Op("get_pennylane_category", "get", "categories/{id}", "One analytical category."),
    Op(
        "create_pennylane_category",
        "post",
        "categories",
        "Create an analytical category.",
        (("label", S), ("category_group_id", I), ("analytical_code", S)),
        ("label", "category_group_id"),
    ),
    Op(
        "update_pennylane_category",
        "put",
        "categories/{id}",
        "Update an analytical category.",
        (("label", S), ("analytical_code", S)),
    ),
    Op("get_pennylane_category_group", "get", "category_groups/{id}", "One analytical category group."),
    Op(
        "create_pennylane_category_group",
        "post",
        "category_groups",
        "Create an analytical category group.",
        (("label", S),),
        ("label",),
    ),
    Op(
        "update_pennylane_category_group",
        "put",
        "category_groups/{id}",
        "Rename an analytical category group.",
        (("label", S),),
        ("label",),
    ),
    Op(
        "get_pennylane_invoice_categories",
        "get",
        "{kind}_invoices/{id}/categories",
        "Analytical categories of a customer or supplier invoice.",
    ),
    Op(
        "get_pennylane_transaction_categories",
        "get",
        "transactions/{id}/categories",
        "Analytical categories of a bank transaction.",
    ),
    Op(
        "get_pennylane_party_categories",
        "get",
        "{party}s/{id}/categories",
        "Analytical categories of a customer or supplier.",
    ),
    Op(
        "set_pennylane_party_categories",
        "put",
        "{party}s/{id}/categories",
        "Set the analytical categories of a customer or supplier.",
        (("items", WEIGHTS),),
        ("items",),
        list_body=True,
    ),
    # --- Products --------------------------------------------------------------
    Op("get_pennylane_product", "get", "products/{id}", "One product."),
    Op(
        "create_pennylane_product",
        "post",
        "products",
        "Create a product.",
        (
            ("label", S),
            ("price_before_tax", S),
            ("vat_rate", S),
            ("unit", S),
            ("currency", S),
            ("reference", S),
            ("description", S),
            ("ledger_account_id", I),
        ),
        ("label", "price_before_tax", "vat_rate", "unit"),
    ),
    Op(
        "update_pennylane_product",
        "put",
        "products/{id}",
        "Update a product.",
        (("label", S), ("price_before_tax", S), ("vat_rate", S), ("unit", S), ("description", S)),
    ),
    # --- Customers: contacts, e-invoicing --------------------------------------
    Op("list_pennylane_customer_contacts", "get", "customers/{id}/contacts", "Contacts of a customer.", paged=True),
    Op("get_pennylane_customer_contact", "get", "customers/{id}/contacts/{contact_id}", "One contact of a customer."),
    Op(
        "create_pennylane_customer_contact",
        "post",
        "customers/{id}/contacts",
        "Add a contact to a customer.",
        (("first_name", S), ("last_name", S), ("email", S), ("phone", S)),
    ),
    Op(
        "update_pennylane_customer_contact",
        "put",
        "customers/{id}/contacts/{contact_id}",
        "Update a customer contact.",
        (("first_name", S), ("last_name", S), ("email", S), ("phone", S)),
    ),
    Op(
        "delete_pennylane_customer_contact",
        "delete",
        "customers/{id}/contacts/{contact_id}",
        "Delete a customer contact.",
        always_ask=True,
    ),
    Op(
        "list_pennylane_customer_e_invoicing_addresses",
        "get",
        "customers/{id}/electronic_billing_addresses",
        "Electronic invoicing addresses of a customer.",
        paged=True,
    ),
    # --- Customer invoices lifecycle -------------------------------------------
    Op(
        "list_pennylane_invoice_sections",
        "get",
        "customer_invoices/{id}/invoice_line_sections",
        "Line sections of a customer invoice.",
        paged=True,
    ),
    Op(
        "list_pennylane_invoice_appendices",
        "get",
        "{kind}_invoices/{id}/appendices",
        "Appendices of a customer invoice.",
        paged=True,
    ),
    Op(
        "add_pennylane_invoice_appendix",
        "post",
        "customer_invoices/{id}/appendices",
        "Attach an appendix (file_attachment_id) to a customer invoice.",
        (("file_attachment_id", I),),
        ("file_attachment_id",),
    ),
    Op(
        "list_pennylane_invoice_custom_fields",
        "get",
        "customer_invoices/{id}/custom_header_fields",
        "Custom header fields of a customer invoice.",
        paged=True,
    ),
    Op(
        "update_pennylane_customer_invoice",
        "put",
        "customer_invoices/{id}",
        "Update a draft customer invoice (label, dates, lines, customer, texts).",
        (
            ("label", S),
            ("date", S),
            ("deadline", S),
            ("customer_id", I),
            ("invoice_lines", A),
            ("pdf_invoice_subject", S),
            ("pdf_invoice_free_text", S),
            ("external_reference", S),
        ),
    ),
    Op(
        "finalize_pennylane_customer_invoice",
        "put",
        "customer_invoices/{id}/finalize",
        "Finalize a draft customer invoice (gives it its number; cannot be undone).",
        always_ask=True,
    ),
    Op(
        "delete_pennylane_customer_invoice",
        "delete",
        "customer_invoices/{id}",
        "Delete a draft customer invoice.",
        always_ask=True,
    ),
    Op(
        "send_pennylane_customer_invoice",
        "post",
        "customer_invoices/{id}/send_by_email",
        "Email a finalized customer invoice to its customer.",
        (("recipients", {"type": "array", "items": S}),),
        always_ask=True,
    ),
    Op(
        "send_pennylane_customer_invoice_to_pa",
        "post",
        "customer_invoices/{id}/send_to_pa",
        "Send a customer invoice to the e-invoicing platform (PA).",
        always_ask=True,
    ),
    Op(
        "create_pennylane_customer_invoice_from_quote",
        "post",
        "customer_invoices/create_from_quote",
        "Create a draft customer invoice from a quote.",
        (("quote_id", I), ("draft", B)),
        ("quote_id",),
    ),
    Op(
        "import_pennylane_customer_invoice",
        "post",
        "customer_invoices/import",
        "Import an existing customer invoice from an uploaded PDF (file_attachment_id) with its amounts.",
        (
            ("file_attachment_id", I),
            ("customer_id", I),
            ("date", S),
            ("deadline", S),
            ("invoice_number", S),
            ("currency_amount", S),
            ("currency_amount_before_tax", S),
            ("currency_tax", S),
            ("invoice_lines", A),
        ),
        ("file_attachment_id", "customer_id", "date", "deadline"),
    ),
    Op(
        "import_pennylane_customer_e_invoice",
        "post",
        "customer_invoices/e_invoices/imports",
        "Import a customer e-invoice (Factur-X file_attachment_id).",
        (("file_attachment_id", I),),
        ("file_attachment_id",),
    ),
    Op(
        "update_pennylane_imported_customer_invoice",
        "put",
        "customer_invoices/{id}/update_imported",
        "Update an imported customer invoice.",
        (("invoice_number", S), ("date", S), ("deadline", S), ("currency_amount", S), ("invoice_lines", A)),
    ),
    Op(
        "link_pennylane_credit_note",
        "post",
        "customer_invoices/{id}/link_credit_note",
        "Link a credit note to the customer invoice it credits.",
        (("credit_note_id", I),),
        ("credit_note_id",),
    ),
    Op(
        "list_pennylane_invoice_templates",
        "get",
        "customer_invoice_templates",
        "Customer invoice templates.",
        paged=True,
    ),
    Op(
        "list_pennylane_billing_email_templates",
        "get",
        "billing_email_templates",
        "Billing email templates.",
        paged=True,
    ),
    Op("list_pennylane_numberings", "get", "numberings", "Document numbering sequences.", paged=True),
    Op(
        "list_pennylane_pa_registrations",
        "get",
        "pa_registrations",
        "E-invoicing platform (PA) registrations.",
        paged=True,
    ),
    # --- Supplier invoices lifecycle -------------------------------------------
    Op(
        "list_pennylane_supplier_invoice_payments",
        "get",
        "supplier_invoices/{id}/payments",
        "Payments of a supplier invoice.",
        paged=True,
    ),
    Op(
        "import_pennylane_supplier_invoice",
        "post",
        "supplier_invoices/import",
        "Import a supplier invoice from an uploaded file (file_attachment_id) with its amounts and lines.",
        (
            ("file_attachment_id", I),
            ("supplier_id", I),
            ("date", S),
            ("deadline", S),
            ("invoice_number", S),
            ("currency", S),
            ("currency_amount", S),
            ("currency_amount_before_tax", S),
            ("currency_tax", S),
            ("label", S),
            ("invoice_lines", A),
            ("import_as_incomplete", B),
        ),
        (
            "file_attachment_id",
            "supplier_id",
            "date",
            "deadline",
            "currency_amount_before_tax",
            "currency_amount",
            "currency_tax",
            "invoice_lines",
        ),
    ),
    Op(
        "import_pennylane_supplier_e_invoice",
        "post",
        "supplier_invoices/e_invoices/imports",
        "Import a supplier e-invoice (file_attachment_id).",
        (("file_attachment_id", I),),
        ("file_attachment_id",),
    ),
    Op(
        "set_pennylane_supplier_e_invoice_status",
        "put",
        "supplier_invoices/{id}/e_invoice_status",
        "Set the e-invoicing status of a supplier invoice (accept or refuse it).",
        (("status", S), ("reason", S)),
        ("status",),
        always_ask=True,
    ),
    Op(
        "link_pennylane_purchase_request",
        "post",
        "supplier_invoices/{id}/linked_purchase_requests",
        "Link purchase requests to a supplier invoice.",
        (("purchase_request_ids", {"type": "array", "items": I}),),
        ("purchase_request_ids",),
    ),
    Op("list_pennylane_purchase_requests", "get", "purchase_requests", "Purchase requests.", query=FILTER, paged=True),
    Op("get_pennylane_purchase_request", "get", "purchase_requests/{id}", "One purchase request."),
    Op(
        "import_pennylane_purchase_requests",
        "post",
        "purchase_requests/imports",
        "Import purchase requests.",
        (("purchase_requests", A),),
        ("purchase_requests",),
    ),
    # --- Quotes and commercial documents ---------------------------------------
    Op("get_pennylane_quote", "get", "quotes/{id}", "One quote."),
    Op("list_pennylane_quote_lines", "get", "quotes/{id}/invoice_lines", "Lines of a quote.", paged=True),
    Op(
        "list_pennylane_quote_sections",
        "get",
        "quotes/{id}/invoice_line_sections",
        "Line sections of a quote.",
        paged=True,
    ),
    Op("list_pennylane_quote_appendices", "get", "quotes/{id}/appendices", "Appendices of a quote.", paged=True),
    Op(
        "create_pennylane_quote",
        "post",
        "quotes",
        "Create a quote.",
        (
            ("customer_id", I),
            ("date", S),
            ("deadline", S),
            ("invoice_lines", A),
            ("label", S),
            ("currency", S),
            ("pdf_invoice_subject", S),
        ),
        ("customer_id", "date", "deadline", "invoice_lines"),
    ),
    Op(
        "update_pennylane_quote",
        "put",
        "quotes/{id}",
        "Update a quote.",
        (("date", S), ("deadline", S), ("invoice_lines", A), ("label", S), ("pdf_invoice_subject", S)),
    ),
    Op(
        "set_pennylane_quote_status",
        "put",
        "quotes/{id}/update_status",
        "Set a quote's status (pending, accepted, denied).",
        (("status", S),),
        ("status",),
    ),
    Op(
        "add_pennylane_quote_appendix",
        "post",
        "quotes/{id}/appendices",
        "Attach an appendix (file_attachment_id) to a quote.",
        (("file_attachment_id", I),),
        ("file_attachment_id",),
    ),
    Op(
        "send_pennylane_quote",
        "post",
        "quotes/{id}/send_by_email",
        "Email a quote to its customer.",
        (("recipients", {"type": "array", "items": S}),),
        always_ask=True,
    ),
    Op(
        "list_pennylane_commercial_documents",
        "get",
        "commercial_documents",
        "Commercial documents (orders, delivery notes...).",
        query=FILTER,
        paged=True,
    ),
    Op("get_pennylane_commercial_document", "get", "commercial_documents/{id}", "One commercial document."),
    Op(
        "list_pennylane_commercial_document_lines",
        "get",
        "commercial_documents/{id}/invoice_lines",
        "Lines of a commercial document.",
        paged=True,
    ),
    Op(
        "list_pennylane_commercial_document_sections",
        "get",
        "commercial_documents/{id}/invoice_line_sections",
        "Line sections of a commercial document.",
        paged=True,
    ),
    Op(
        "list_pennylane_commercial_document_appendices",
        "get",
        "commercial_documents/{id}/appendices",
        "Appendices of a commercial document.",
        paged=True,
    ),
    Op(
        "create_pennylane_commercial_document",
        "post",
        "commercial_documents",
        "Create a commercial document.",
        (("customer_id", I), ("date", S), ("deadline", S), ("invoice_lines", A), ("label", S)),
        ("customer_id", "date", "invoice_lines"),
    ),
    Op(
        "update_pennylane_commercial_document",
        "put",
        "commercial_documents/{id}",
        "Update a commercial document.",
        (("date", S), ("deadline", S), ("invoice_lines", A), ("label", S)),
    ),
    Op(
        "add_pennylane_commercial_document_appendix",
        "post",
        "commercial_documents/{id}/appendices",
        "Attach an appendix to a commercial document.",
        (("file_attachment_id", I),),
        ("file_attachment_id",),
    ),
    Op(
        "send_pennylane_commercial_document",
        "post",
        "commercial_documents/{id}/send_by_email",
        "Email a commercial document to its customer.",
        (("recipients", {"type": "array", "items": S}),),
        always_ask=True,
    ),
    # --- Billing subscriptions -------------------------------------------------
    Op(
        "list_pennylane_billing_subscriptions",
        "get",
        "billing_subscriptions",
        "Billing subscriptions (recurring invoices).",
        query=FILTER,
        paged=True,
    ),
    Op("get_pennylane_billing_subscription", "get", "billing_subscriptions/{id}", "One billing subscription."),
    Op(
        "list_pennylane_billing_subscription_lines",
        "get",
        "billing_subscriptions/{id}/invoice_lines",
        "Lines of a billing subscription.",
        paged=True,
    ),
    Op(
        "list_pennylane_billing_subscription_sections",
        "get",
        "billing_subscriptions/{id}/invoice_line_sections",
        "Line sections of a billing subscription.",
        paged=True,
    ),
    Op(
        "create_pennylane_billing_subscription",
        "post",
        "billing_subscriptions",
        "Create a billing subscription (recurring customer invoices).",
        (
            ("customer_id", I),
            ("start", S),
            ("recurring_rule", O),
            ("invoice_lines", A),
            ("label", S),
            ("payment_conditions", S),
            ("mode", S),
        ),
        ("customer_id", "start", "recurring_rule", "invoice_lines"),
        always_ask=True,
    ),
    Op(
        "update_pennylane_billing_subscription",
        "put",
        "billing_subscriptions/{id}",
        "Update or stop a billing subscription.",
        (("status", S), ("recurring_rule", O), ("invoice_lines", A), ("label", S)),
        always_ask=True,
    ),
    # --- Banking: transactions, accounts, establishments -----------------------
    Op("get_pennylane_transaction", "get", "transactions/{id}", "One bank transaction."),
    Op(
        "list_pennylane_transaction_matches",
        "get",
        "transactions/{id}/matched_invoices",
        "Invoices a bank transaction is matched to.",
        paged=True,
    ),
    Op(
        "create_pennylane_transaction",
        "post",
        "transactions",
        "Record a bank transaction on a bank account (bookkeeping only, no money moves).",
        (("bank_account_id", I), ("label", S), ("date", S), ("amount", S), ("currency", S), ("fee", S)),
        ("bank_account_id", "label", "date", "amount"),
        always_ask=True,
        bank=True,
    ),
    Op(
        "update_pennylane_transaction",
        "put",
        "transactions/{id}",
        "Update a bank transaction's label or counterpart.",
        (("label", S), ("supplier_id", I), ("customer_id", I)),
    ),
    Op("get_pennylane_bank_account", "get", "bank_accounts/{id}", "One bank account.", bank=True),
    Op(
        "create_pennylane_bank_account",
        "post",
        "bank_accounts",
        "Create a bank account (name, currency, IBAN details).",
        (("name", S), ("currency", S), ("iban", S), ("bic", S), ("journal_id", I), ("ledger_account_id", I)),
        ("name",),
        always_ask=True,
        bank=True,
    ),
    Op(
        "list_pennylane_bank_establishments",
        "get",
        "bank_establishments",
        "Bank establishments known to Pennylane.",
        query=FILTER,
        paged=True,
        bank=True,
    ),
    # --- Mandates --------------------------------------------------------------
    Op(
        "list_pennylane_sepa_mandates",
        "get",
        "sepa_mandates",
        "SEPA direct debit mandates of customers.",
        query=FILTER,
        paged=True,
        bank=True,
    ),
    Op("get_pennylane_sepa_mandate", "get", "sepa_mandates/{id}", "One SEPA mandate.", bank=True),
    Op(
        "create_pennylane_sepa_mandate",
        "post",
        "sepa_mandates",
        "Record a customer's SEPA direct debit mandate.",
        (("customer_id", I), ("iban", S), ("bic", S), ("reference", S), ("signed_at", S), ("sequence_type", S)),
        ("customer_id", "iban"),
        always_ask=True,
        bank=True,
    ),
    Op(
        "update_pennylane_sepa_mandate",
        "put",
        "sepa_mandates/{id}",
        "Update a SEPA mandate.",
        (("iban", S), ("bic", S), ("reference", S), ("signed_at", S), ("sequence_type", S)),
        always_ask=True,
        bank=True,
    ),
    Op(
        "delete_pennylane_sepa_mandate",
        "delete",
        "sepa_mandates/{id}",
        "Delete a SEPA mandate.",
        always_ask=True,
        bank=True,
    ),
    Op(
        "list_pennylane_gocardless_mandates",
        "get",
        "gocardless_mandates",
        "GoCardless mandates.",
        query=FILTER,
        paged=True,
        bank=True,
    ),
    Op("get_pennylane_gocardless_mandate", "get", "gocardless_mandates/{id}", "One GoCardless mandate.", bank=True),
    Op(
        "request_pennylane_gocardless_mandate",
        "post",
        "gocardless_mandates/mail_requests",
        "Email a customer a GoCardless mandate request.",
        (("customer_id", I), ("recipient", S)),
        ("customer_id",),
        always_ask=True,
        bank=True,
    ),
    Op(
        "cancel_pennylane_gocardless_mandate",
        "post",
        "gocardless_mandates/{id}/cancellations",
        "Cancel a GoCardless mandate.",
        always_ask=True,
        bank=True,
    ),
    Op(
        "associate_pennylane_gocardless_mandate",
        "post",
        "gocardless_mandates/{id}/associations",
        "Associate a GoCardless mandate with a customer.",
        (("customer_id", I),),
        ("customer_id",),
        always_ask=True,
        bank=True,
    ),
    Op(
        "list_pennylane_pro_account_mandates",
        "get",
        "pro_account/mandates",
        "Pennylane Pro account mandates.",
        query=FILTER,
        paged=True,
        bank=True,
    ),
    Op(
        "request_pennylane_pro_account_mandate",
        "post",
        "pro_account/mandate_requests",
        "Email a customer a Pro account mandate request.",
        (("customer_id", I), ("recipient", S)),
        ("customer_id",),
        always_ask=True,
        bank=True,
    ),
    Op(
        "list_pennylane_pro_account_mandate_migrations",
        "get",
        "pro_account/mandate_migrations",
        "Pro account mandate migrations.",
        paged=True,
        bank=True,
    ),
    Op(
        "migrate_pennylane_pro_account_mandates",
        "post",
        "pro_account/mandate_migrations",
        "Migrate mandates to the Pro account.",
        (("mandate_ids", {"type": "array", "items": I}),),
        always_ask=True,
        bank=True,
    ),
    # --- Webhooks --------------------------------------------------------------
    Op(
        "list_pennylane_webhook_subscriptions",
        "get",
        "webhook_subscriptions",
        "Webhook subscriptions of the token.",
        paged=True,
    ),
    Op("get_pennylane_webhook_subscription", "get", "webhook_subscriptions/{id}", "One webhook subscription."),
    Op(
        "create_pennylane_webhook_subscription",
        "post",
        "webhook_subscriptions",
        "Subscribe a URL to Pennylane events.",
        (("url", S), ("events", {"type": "array", "items": S})),
        ("url", "events"),
        always_ask=True,
    ),
    Op(
        "update_pennylane_webhook_subscription",
        "put",
        "webhook_subscriptions/{id}",
        "Update a webhook subscription.",
        (("url", S), ("events", {"type": "array", "items": S})),
        always_ask=True,
    ),
    Op(
        "delete_pennylane_webhook_subscription",
        "delete",
        "webhook_subscriptions/{id}",
        "Delete a webhook subscription.",
        always_ask=True,
    ),
)

#: Tools showing or changing bank accounts or mandates: one group a binding can withhold.
CATALOG_BANK_TOOLS = frozenset(op.name for op in OPS if op.bank)

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_ENUM_PLACEHOLDERS = {"kind": ("customer", "supplier"), "party": ("customer", "supplier")}


def _render(data: Any) -> str:
    """A compact readable view of an answer whatever its shape."""
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    if isinstance(data, list):
        if not data:
            return "Nothing."
        return f"{len(data)} records:\n" + "\n".join(f"- {_one(i)}" for i in data[:100])
    if isinstance(data, dict):
        return _one(data, limit=4000) if data else "Done."
    return text(data, 2000)


def _one(item: Any, limit: int = 600) -> str:
    if not isinstance(item, dict):
        return text(item, limit)
    parts: list[tuple[str, Any]] = []
    for key, value in item.items():
        if value in (None, "", [], {}) or key in ("url", "public_file_url", "created_at"):
            continue
        if key == "iban":
            raw = "".join(str(value).split())
            value = f"{raw[:2]}** **** {raw[-4:]}" if len(raw) >= 8 else "****"
        elif isinstance(value, dict):
            value = (
                value.get("name")
                or value.get("label")
                or (f"#{value['id']}" if "id" in value else json.dumps(value)[:120])
            )
        elif isinstance(value, list):
            value = f"{len(value)} item(s)"
        parts.append((key, text(value, 160)))
    return text(line(parts), limit)


def _make(ctx: PennylaneContext, op: Op) -> Any:
    placeholders = _PLACEHOLDER.findall(op.path)
    body_names = [name for name, _ in op.body]
    query_names = [name for name, _ in op.query]

    async def tool(**kwargs: Any) -> str:
        values: dict[str, str] = {}
        for name in placeholders:
            raw = kwargs.get(name)
            allowed = _ENUM_PLACEHOLDERS.get(name)
            if allowed is not None:
                if raw not in allowed:
                    return f"Error: {name} must be one of {', '.join(allowed)}."
                values[name] = str(raw)
            elif raw in (None, "") or not str(raw).isdigit():
                return f"Error: {name} is required (a Pennylane id)."
            else:
                values[name] = str(int(raw))
        path = op.path.format(**values)
        missing = [r for r in op.required if kwargs.get(r) in (None, "", [])]
        if missing:
            return f"Error: {', '.join(missing)} required."
        async with ctx.client() as client:
            if op.method == "get":
                params = {n: kwargs.get(n) for n in query_names if kwargs.get(n) not in (None, "")}
                if op.paged:
                    page, err = await ctx.page(
                        client,
                        path,
                        params=params,
                        limit=clamp(kwargs.get("limit"), 25, 100),
                        cursor=str(kwargs.get("cursor") or ""),
                    )
                    return err or (_render(page.items) + page_footer(page))
                data, err = await ctx.get(client, path, params)
                return err or _render(data)
            body: Any
            if op.list_body:
                body = kwargs.get("items")
            else:
                body = {n: kwargs[n] for n in body_names if kwargs.get(n) not in (None, "")}
            data, err = await ctx.request(
                client,
                op.method,
                path,
                body=body if (body or op.method != "delete") else None,
                action=op.name.replace("_", " "),
            )
        if err:
            return err
        return f"Done: {op.description.rstrip('.')} ({op.method.upper()} {path}).\n{_render(data)}"

    tool.__name__ = op.name
    return tool


def _schema(op: Op) -> tuple[dict[str, Any], list[str]]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for name in _PLACEHOLDER.findall(op.path):
        if name in _ENUM_PLACEHOLDERS:
            props[name] = {"type": "string", "enum": list(_ENUM_PLACEHOLDERS[name])}
        else:
            props[name] = {"type": "integer", "description": "Pennylane id"}
        required.append(name)
    props.update(dict(op.query))
    props.update(dict(op.body))
    required += [r for r in op.required if r not in required]
    if op.paged:
        props["limit"] = LIMIT
        props["cursor"] = CURSOR
    return props, required


def register(ctx: PennylaneContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    for op in OPS:
        executor.register(op.name, _make(ctx, op))
        props, required = _schema(op)
        description = op.description + (" Waits for a person's approval." if op.always_ask else "")
        tools.append(
            definition(
                op.name,
                description,
                props,
                required,
                priority=2,
                read_only=op.method == "get",
                always_ask=op.always_ask,
            )
        )
    return tools
