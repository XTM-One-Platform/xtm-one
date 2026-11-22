# SPDX-License-Identifier: Apache-2.0

"""Every documented operation of the DealHub CPQ APIs, as tools.

Source: https://developers.dealhub.io/llms.txt (Quote, Headless, CRM, User,
Partner, Pricing and Version APIs; read 21 Nov 2026). One row per operation:
the tool name, the HTTP method and path, the path and query parameters and
whether it takes a JSON body. Read or write follows the HTTP method: a GET is
a read, anything else is a write that asks a person before it runs, a POST
DealHub documents as a lookup included (the method decides, never the name).

Left out on purpose, because DealHub calls them and no client can:
the Callout API (``getPricing``, DealHub asks your pricing service) and the
External Query (``fulfillExternalQuery``, DealHub asks your data source).
The Subskribe billing API (subscriptions, invoices, renewals) has its own
credential and host; it is not part of this provider.
DealHub has no endpoint to approve or reject an approval step: approvals are
read on the quote (``feature=approvals``) and decided in DealHub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The ``feature`` values of the quote reads.
QUOTE_FEATURES = ("info", "summary", "deal_room_info", "line_items", "answers", "approvals", "all")


@dataclass(frozen=True)
class Param:
    name: str
    where: str  # "path" or "query"
    description: str
    required: bool = False
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "string"})


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    description: str
    params: tuple[Param, ...] = ()
    body: str | None = None  # the body's description; None: no body
    body_required: bool = False
    paginated: bool = False
    file: str | None = None  # "document" or "excel": the answer is a file

    @property
    def is_read(self) -> bool:
        return self.method == "GET"


def _p(name: str, description: str, *, where: str = "path", required: bool | None = None, **schema: Any) -> Param:
    return Param(
        name=name,
        where=where,
        description=description,
        required=(where == "path") if required is None else required,
        schema={"type": "string", **schema} if schema else {"type": "string"},
    )


_FEATURE = Param(
    "feature",
    "query",
    "Parts of the quote to return: " + ", ".join(QUOTE_FEATURES) + " (default info).",
    schema={"type": "array", "items": {"type": "string", "enum": list(QUOTE_FEATURES)}},
)
_OFFSET = Param("offset", "query", "First item to return (default 0).", schema={"type": "integer", "minimum": 0})
_LIMIT = Param("limit", "query", "Items per page (default 50).", schema={"type": "integer", "minimum": 1})
_QUOTE_ID = _p("dealhub_quote_id", "DealHub quote id.")
_VERSION_ID = _p("version_id", "DealHub version id.")

OPERATIONS: tuple[Operation, ...] = (
    # ── Quote API ────────────────────────────────────────────────────────
    Operation(
        "list_dealhub_quotes",
        "GET",
        "/api/v2/quotes",
        "List quotes: status, owner, totals, discounts, line items, playbook answers and approval steps "
        "by feature. external_opportunity_id keeps the quotes of one CRM opportunity.",
        (_p("external_opportunity_id", "CRM opportunity id.", where="query"), _FEATURE, _OFFSET, _LIMIT),
        paginated=True,
    ),
    Operation(
        "get_dealhub_quote",
        "GET",
        "/api/v2/quote/{quote_id}",
        "Read one quote: info (status, owner, dates, currency, playbook), summary (list and net totals, "
        "discounts), DealRoom link, line items, playbook answers, approval steps with reviewer and status.",
        (
            _p("quote_id", "DealHub quote id, or the CRM quote id with external_identifier_flag."),
            _FEATURE,
            Param("external_identifier_flag", "query", "quote_id is the CRM quote id.", schema={"type": "boolean"}),
        ),
    ),
    Operation(
        "download_dealhub_quote_document",
        "GET",
        "/api/v1/quote/{dealhub_quote_id}/document",
        "Download the generated document of a quote (PDF, WORD or EXCEL) as a file of the conversation.",
        (_QUOTE_ID, _p("document_type", "PDF (default), WORD or EXCEL.", where="query", enum=["PDF", "WORD", "EXCEL"])),
        file="document",
    ),
    Operation(
        "get_dealhub_dealroom_signers",
        "GET",
        "/api/v1/dealroom/{opportunity_id}",
        "Read the DealRoom of an opportunity: its signers and their signing state.",
        (_p("opportunity_id", "DealHub or CRM opportunity id."),),
    ),
    Operation(
        "create_dealhub_opportunity",
        "POST",
        "/api/v1/opportunity",
        "Create an opportunity in DealHub (external id and name, owner, currency, customer).",
        body="external_opportunity_id, external_opportunity_name, opportunity_owner, currency, "
        "external_customer_id, customer_name, and the optional fields DealHub documents.",
        body_required=True,
    ),
    # ── Headless API ─────────────────────────────────────────────────────
    Operation(
        "get_dealhub_quote_id",
        "GET",
        "/api/v1/quote/id/{dealhub_proposal_id}",
        "Find the quote id of a DealHub proposal id.",
        (_p("dealhub_proposal_id", "DealHub proposal id."),),
    ),
    Operation(
        "simulate_dealhub_quote",
        "POST",
        "/api/v1/quote/simulate",
        "Price a quote without saving it: line items, discounts and totals for the playbook answers given.",
        body="quote_data (required), version_id, external_opportunity_id, geo_code, currency, playbook, "
        "line_items, line_item_details, total_sales_discount, partner_program.",
        body_required=True,
    ),
    Operation(
        "generate_dealhub_quote",
        "POST",
        "/api/v1/quote/generate",
        "Create a quote from playbook answers and line items; request_action saves, submits or publishes it.",
        body="external_opportunity_id, request_action, document_type, quote_data (required), playbook, "
        "quote_name, line_items, total_sales_discount, expiration_date, opportunity_data and the rest.",
        body_required=True,
    ),
    Operation(
        "submit_dealhub_quote",
        "POST",
        "/api/v1/quote/{dealhub_quote_id}/submit",
        "Submit a quote: it goes to its approval workflow when its discount or terms need one.",
        (_QUOTE_ID,),
        body="Optional line_item_details.",
    ),
    Operation(
        "publish_dealhub_quote",
        "POST",
        "/api/v1/quote/{dealhub_quote_id}/publish",
        "Publish an approved quote to its DealRoom for the buyer.",
        (_QUOTE_ID,),
        body="Optional line_item_details.",
    ),
    Operation(
        "sign_dealhub_quote_externally",
        "POST",
        "/api/v1/quote/{dealhub_quote_id}/sign_externally",
        "Mark a quote signed outside DealHub (wet ink or another e-signature tool).",
        (_QUOTE_ID,),
        body="Optional comment.",
    ),
    # ── CRM API ──────────────────────────────────────────────────────────
    Operation(
        "authenticate_dealhub_user",
        "POST",
        "/api/v1/user/authenticate",
        "Authenticate a DealHub user for a CRM session; answers their DealHub user id.",
        body="user_information (required).",
        body_required=True,
    ),
    Operation(
        "create_dealhub_quote",
        "POST",
        "/api/v1.1/quote/create",
        "Start a new quote on a CRM opportunity; answers the URL that opens it in DealHub.",
        body="dealhub_user_id, external_opportunity_id, opportunity_info, as DealHub documents.",
        body_required=True,
    ),
    Operation(
        "open_dealhub_quote",
        "POST",
        "/api/v2/quote/open",
        "Answer the URL that opens an existing quote in DealHub for a user.",
        body="dealhub_user_id, dealhub_quote_id, external_opportunity_id (required), opportunity_info.",
        body_required=True,
    ),
    Operation(
        "open_dealhub_opportunity_quotes",
        "POST",
        "/api/v2/quotes",
        "Answer the URL of the quotes page of a CRM opportunity for a user, creating the opportunity if new.",
        body="dealhub_user_id, external_opportunity_id, opportunity_info.",
        body_required=True,
    ),
    Operation(
        "open_dealhub_user_opportunities",
        "POST",
        "/api/v1/opportunities",
        "Answer the URL of a user's opportunities page in DealHub.",
        body="dealhub_user_id (required).",
        body_required=True,
    ),
    Operation(
        "start_dealhub_crm_import",
        "POST",
        "/api/v1/accounts/crm-import",
        "Import buyer accounts from the CRM into DealHub; answers a request id.",
        body="buyer_account_crm_ids: the CRM account ids.",
        body_required=True,
    ),
    Operation(
        "get_dealhub_crm_import_status",
        "GET",
        "/api/v1/accounts/crm-import/status",
        "Read the state of a CRM account import.",
        (_p("request_id", "Import request id.", where="query"),),
    ),
    Operation(
        "get_dealhub_crm_import_detail",
        "GET",
        "/api/v1/accounts/crm-import/detail",
        "Read the per-account result of a CRM account import.",
        (_p("request_id", "Import request id.", where="query"),),
    ),
    Operation(
        "lookup_dealhub_crm_import",
        "GET",
        "/api/v1/accounts/crm-import/lookup",
        "Find the import of one CRM account.",
        (
            _p("crm_id", "CRM account id.", where="query", required=True),
            _p("request_id", "Import request id.", where="query"),
        ),
    ),
    Operation(
        "retry_dealhub_crm_import",
        "POST",
        "/api/v1/accounts/crm-import/retry",
        "Retry the failed accounts of a CRM account import.",
        (_p("request_id", "Import request id.", where="query"),),
    ),
    # ── User API ─────────────────────────────────────────────────────────
    Operation(
        "list_dealhub_users",
        "GET",
        "/api/v1/users",
        "List DealHub users: login, name, email, manager, active state.",
        (_p("status", "active or inactive.", where="query", enum=["active", "inactive"]),),
    ),
    Operation("get_dealhub_user", "GET", "/api/v1/user/id/{id}", "Read a DealHub user by id.", (_p("id", "User id."),)),
    Operation(
        "get_dealhub_user_by_login",
        "GET",
        "/api/v1/user/login/{login}",
        "Read a DealHub user by login.",
        (_p("login", "User login."),),
    ),
    Operation(
        "update_dealhub_users",
        "PUT",
        "/api/v1/users",
        "Update DealHub users (v1): name, email, position, contact details, manager, active state.",
        body="users: a list of {login, email, name, is_active, position, phone, ..., user_manager_login}.",
        body_required=True,
    ),
    Operation(
        "create_or_update_dealhub_users",
        "PUT",
        "/api/v2/users",
        "Create or update DealHub users (v2), groups and roles included.",
        body="users: a list of users as DealHub documents for v2.",
        body_required=True,
    ),
    # ── Partner API ──────────────────────────────────────────────────────
    Operation(
        "authenticate_dealhub_partner_user",
        "POST",
        "/api/v1/authenticate/user",
        "Authenticate a partner portal user; answers the session used by the partner quote calls.",
        body="authentication and user_information (required).",
        body_required=True,
    ),
    Operation(
        "create_dealhub_partner_quote",
        "POST",
        "/api/v1/create/quote",
        "Answer the URL that creates a partner quote on an opportunity.",
        body="opportunity_id (required).",
        body_required=True,
    ),
    Operation(
        "open_dealhub_partner_quote",
        "POST",
        "/api/v1/open/quote",
        "Answer the URL that opens a partner quote.",
        body="opportunity_id and quote_id (required).",
        body_required=True,
    ),
    Operation(
        "open_dealhub_partner_opportunity",
        "POST",
        "/api/v1/open/opportunity",
        "Answer the URL of the quotes of a partner opportunity.",
        body="opportunity_id (required).",
        body_required=True,
    ),
    # ── Pricing API ──────────────────────────────────────────────────────
    Operation(
        "price_dealhub_products",
        "POST",
        "/api/v1/products/pricing",
        "Price SKUs for a currency and geo (price book), optionally for a version and playbook.",
        body="currency, geo, skus (required), version, playbook.",
        body_required=True,
    ),
    Operation(
        "request_dealhub_product_parameters",
        "POST",
        "/api/v1/products/parameters",
        "Read the product parameters of SKUs for a currency and geo.",
        body="currency, geo, skus (required), version, playbook.",
        body_required=True,
    ),
    # ── Version API ──────────────────────────────────────────────────────
    Operation(
        "list_dealhub_versions",
        "GET",
        "/api/v1/versions",
        "List the configuration versions (catalog, price books, playbooks) and their state.",
        (_p("status", "DRAFT, ACTIVE or DEACTIVATED.", where="query", enum=["DRAFT", "ACTIVE", "DEACTIVATED"]),),
    ),
    Operation("get_dealhub_version", "GET", "/api/v1/version/id/{version_id}", "Read a version by id.", (_VERSION_ID,)),
    Operation(
        "get_dealhub_version_by_name",
        "GET",
        "/api/v1/version/name/{version_name}",
        "Read a version by name.",
        (_p("version_name", "Version name."),),
    ),
    Operation(
        "duplicate_dealhub_version",
        "POST",
        "/api/v1/version/duplicate",
        "Copy a version into a new draft, to change the catalog or pricing without touching the active one.",
        body="version_id, new_version_name (required), comment, to_account_instance.",
        body_required=True,
    ),
    Operation(
        "activate_dealhub_version",
        "POST",
        "/api/v1/version/activate",
        "Activate a version: every new quote uses its catalog, prices and playbooks.",
        body="version_id (required).",
        body_required=True,
    ),
    Operation(
        "export_dealhub_playbook_data",
        "GET",
        "/api/v1/version/{version_name}/playbook/{playbook_id}/export",
        "Export a playbook of a version: its questions, answers and rules.",
        (_p("version_name", "Version name."), _p("playbook_id", "Playbook id.")),
    ),
    Operation(
        "get_dealhub_generate_quote_template",
        "GET",
        "/api/v1/version/{version_id}/playbook/{playbook_id}/generate-quote-template",
        "Read the request template generate_dealhub_quote takes for a playbook.",
        (_VERSION_ID, _p("playbook_id", "Playbook id.")),
    ),
    Operation(
        "list_dealhub_version_products",
        "GET",
        "/api/v1/version/{version_id}/products",
        "List the products of a version with their pricing.",
        (
            _VERSION_ID,
            _OFFSET,
            _LIMIT,
            _p("product_option", "ALL or MODIFIED.", where="query", enum=["ALL", "MODIFIED"]),
            _p("pricing_option", "MIN, MAX or FIRST.", where="query", enum=["MIN", "MAX", "FIRST"]),
        ),
        paginated=True,
    ),
    Operation(
        "request_dealhub_products_by_sku",
        "POST",
        "/api/v1/version/product_by_sku",
        "Read products of a version by SKU.",
        body="sku (required), version_id, pricing_option.",
        body_required=True,
    ),
    Operation(
        "download_dealhub_products_excel",
        "GET",
        "/api/v1/version/products_excel/{version_id}",
        "Download the products of a version as an Excel file of the conversation.",
        (_VERSION_ID,),
        file="excel",
    ),
    Operation(
        "get_dealhub_product_catalog",
        "GET",
        "/api/v1/version/{version_id}/products_catalog",
        "Read the product catalog of a version: products, bundles, SKUs, price books.",
        (
            _VERSION_ID,
            Param("sku", "query", "Keep these SKUs.", schema={"type": "array", "items": {"type": "string"}}),
            _OFFSET,
            _LIMIT,
        ),
        paginated=True,
    ),
    Operation(
        "upload_dealhub_product_catalog",
        "POST",
        "/api/v1/version/{version_id}/products_catalog",
        "Create or update products and bundles of a draft version (asynchronous: answers a request id).",
        (_VERSION_ID,),
        body="products and bundles, as DealHub documents.",
        body_required=True,
    ),
    Operation(
        "update_dealhub_product_catalog",
        "PATCH",
        "/api/v1/version/{version_id}/products_catalog",
        "Update catalog items of a draft version; mode UPDATE changes the fields given, REPLACE replaces items.",
        (_VERSION_ID, _p("mode", "UPDATE or REPLACE.", where="query", required=True, enum=["UPDATE", "REPLACE"])),
        body="products and bundles, as DealHub documents.",
        body_required=True,
    ),
    Operation(
        "delete_dealhub_products_by_sku",
        "DELETE",
        "/api/v1/version/{version_id}/products_catalog/by_sku",
        "Delete products of a draft version by SKU.",
        (_VERSION_ID,),
        body="skus: the SKUs to delete.",
        body_required=True,
    ),
    Operation(
        "delete_all_dealhub_products",
        "DELETE",
        "/api/v1/version/{version_id}/products_catalog/all",
        "Delete every product of a draft version.",
        (_VERSION_ID,),
    ),
    Operation(
        "get_dealhub_async_request_status",
        "GET",
        "/api/v1/request/{request_id}/status",
        "Read the state of an asynchronous catalog request.",
        (_p("request_id", "Request id."),),
    ),
    Operation(
        "get_dealhub_async_request_summary",
        "GET",
        "/api/v1/request/{request_id}/summary",
        "Read the per-item result of an asynchronous catalog request.",
        (_p("request_id", "Request id."),),
    ),
    Operation(
        "get_dealhub_product_hierarchy",
        "GET",
        "/api/v1/version/{version_id}/products_hierarchy",
        "Read the product hierarchy (labels and SKUs) of a version.",
        (_VERSION_ID,),
    ),
    Operation(
        "update_dealhub_product_hierarchy",
        "POST",
        "/api/v1/version/{version_id}/products_hierarchy",
        "Add or move elements of the product hierarchy of a draft version.",
        (_VERSION_ID,),
        body="A list of {element, children}.",
        body_required=True,
    ),
    Operation(
        "delete_from_dealhub_product_hierarchy",
        "DELETE",
        "/api/v1/version/{version_id}/products_hierarchy",
        "Remove labels or SKUs from the product hierarchy of a draft version.",
        (_VERSION_ID,),
        body="items_to_delete: a list of {labelNameOrSku}.",
        body_required=True,
    ),
)
