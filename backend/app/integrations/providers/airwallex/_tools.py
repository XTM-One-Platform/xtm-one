# SPDX-License-Identifier: Apache-2.0

"""The Airwallex tools: a curated operation table, one tool per operation.

Reads cover what a treasury, close or FP&A agent needs (accounts and
entities, global accounts, balances and their history, financial
transactions, deposits, transfers, beneficiaries, FX, cards and card
transactions, invoices, payment intents, settlements, webhooks) plus the
financial reports, exported as a file of the run. Every answer masks bank
account numbers, IBANs and card numbers to their last four characters and
never answers a secret.

Writes (beneficiaries, transfers, FX quotes and conversions, cards and
cardholders, payment links, billing invoices) are always built, named with a
write verb so a read-only binding withholds them, ask a person on every call
(``always_ask``: the money-movement guardrail is a call-time approval) and
carry an idempotency ``request_id`` where Airwallex takes one. Sensitive card
details (full PAN, CVV) are never exposed: the operation is not in the table.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.integrations.file_delivery import deliver_file
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._client import PROVIDER, SOURCE, AirwallexClient, seg

#: The longest text a tool answers; the rest is cut with a note.
MAX_ANSWER_CHARS = 24_000
#: Polls of a financial report's status, and the pause between two.
REPORT_POLLS = 20
REPORT_POLL_SECONDS = 3.0
REDACTED = "[redacted]"

#: Keys whose value is never answered.
_SECRET_KEYS = frozenset(
    {"token", "apikey", "clientsecret", "secret", "password", "cvv", "cvc", "pin", "accesstoken", "webhooksecret"}
)
#: Keys whose value is masked to its last four characters.
_MASKED_KEYS = frozenset(
    {
        "accountnumber",
        "bankaccountnumber",
        "iban",
        "cardnumber",
        "pan",
        "clabe",
        "virtualaccountnumber",
        "accountnumbermasked",
    }
)


def _norm(key: Any) -> str:
    return str(key).replace("_", "").replace("-", "").lower()


def mask(value: Any) -> Any:
    """A bank or card number as its last four characters."""
    if not isinstance(value, (str, int)):
        return value
    text = str(value).replace(" ", "")
    if len(text) <= 4:
        return "****"
    return f"****{text[-4:]}"


def sanitize(data: Any) -> Any:
    """*data* with every secret redacted and every account or card number masked."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            norm = _norm(key)
            if norm in _SECRET_KEYS:
                out[key] = REDACTED
            elif norm in _MASKED_KEYS:
                out[key] = mask(value)
            else:
                out[key] = sanitize(value)
        return out
    if isinstance(data, list):
        return [sanitize(item) for item in data]
    return data


def _page_note(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    if data.get("page_after"):
        return f" (more: pass page={data['page_after']})"
    if data.get("has_more") is True:
        return " (more: raise page_num)"
    if data.get("has_more") is False:
        return " (last page)"
    return ""


def render(title: str, data: Any) -> str:
    """A tool answer: a title line, then the sanitized data as compact JSON, bounded."""
    if data is None:
        return title
    body = json.dumps(sanitize(data), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(body) > MAX_ANSWER_CHARS:
        body = (
            body[:MAX_ANSWER_CHARS]
            + f"... [cut at {MAX_ANSWER_CHARS} of {len(body)} characters: narrow the call with its filters "
            "or page_size]"
        )
    return f"{title}\n{body}"


# --- the operation table -----------------------------------------------------------

_STR = {"type": "string"}
_INT = {"type": "integer", "minimum": 1}


def _p(kind: dict[str, Any], description: str) -> dict[str, Any]:
    return {**kind, "description": description}


_CREATED = {
    "from_created_at": _p(_STR, "Created at or after (ISO 8601, e.g. 2026-11-01T00:00:00+0000)"),
    "to_created_at": _p(_STR, "Created before (ISO 8601)"),
}
_PAGE_NUM = {
    "page_num": _p({"type": "integer", "minimum": 0}, "Page number, from 0"),
    "page_size": _p({**_INT, "maximum": 1000}, "Rows per page (default 100)"),
}
_CURSOR = {
    "page": _p(_STR, "Page cursor: the page_after (or page_before) value of the previous answer"),
    "page_size": _p({**_INT, "maximum": 1000}, "Rows per page (default 100)"),
}
_ON_BEHALF_OF = _p(
    _STR,
    "Connected account id to act on (x-on-behalf-of), for a platform or organisation key; "
    "list them with list_airwallex_connected_accounts. Omit for the integration's own account.",
)


@dataclass(frozen=True)
class Operation:
    """One tool: its name, the call, its arguments and how it answers."""

    name: str
    summary: str
    method: str
    path: str
    query: dict[str, dict[str, Any]] = field(default_factory=dict)
    path_params: tuple[str, ...] = ()
    body: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    write: bool = False
    #: A write's fixed body fields, merged over the arguments.
    fixed_body: dict[str, Any] = field(default_factory=dict)
    essential: bool = False
    #: The family of a write (beneficiaries, transfers, fx, cards, payments, billing).
    group: str = ""


def _get(
    name: str,
    summary: str,
    path: str,
    *,
    query: dict[str, dict[str, Any]] | None = None,
    required: tuple[str, ...] = (),
    essential: bool = False,
) -> Operation:
    path_params = tuple(part[1:-1] for part in path.split("/") if part.startswith("{"))
    return Operation(
        name=name,
        summary=summary,
        method="GET",
        path=path,
        query=dict(query or {}),
        path_params=path_params,
        required=tuple(path_params) + tuple(required),
        essential=essential,
    )


_ID = "/{id}"

READ_OPERATIONS: tuple[Operation, ...] = (
    _get(
        "get_airwallex_account",
        "Account details: legal entity, status, business details",
        "/api/v1/account",
        essential=True,
    ),
    _get("get_airwallex_wallet_info", "Wallet information of the account", "/api/v1/account/wallet_info"),
    _get(
        "list_airwallex_connected_accounts",
        "Connected accounts (entities) a platform or organisation key reaches",
        "/api/v1/accounts",
        query={
            "account_status": _p(_STR, "CREATED, SUBMITTED, ACTION_REQUIRED, ACTIVE, SUSPENDED"),
            **_CREATED,
            **_PAGE_NUM,
        },
        essential=True,
    ),
    _get("get_airwallex_connected_account", "One connected account", "/api/v1/accounts" + _ID),
    _get(
        "list_airwallex_balances",
        "Current balances by currency: available, pending, reserved and total amounts",
        "/api/v1/balances/current",
        essential=True,
    ),
    _get(
        "list_airwallex_balance_history",
        "Balance history: every posted movement with its amount, currency, balance after and source",
        "/api/v1/balances/history",
        query={
            "currency": _p(_STR, "Currency (ISO 4217)"),
            "from_post_at": _p(_STR, "Posted at or after (ISO 8601)"),
            "to_post_at": _p(_STR, "Posted before (ISO 8601)"),
            **_PAGE_NUM,
        },
        essential=True,
    ),
    _get(
        "list_airwallex_financial_transactions",
        "Financial transactions: amount, fee, net, currency, type, status, source and settlement date",
        "/api/v1/financial_transactions",
        query={
            "currency": _p(_STR, "Currency (ISO 4217)"),
            "status": _p(_STR, "PENDING, SETTLED or CANCELLED"),
            "source_id": _p(_STR, "The source object (transfer, deposit, conversion...) id"),
            "batch_id": _p(_STR, "Settlement batch id"),
            **_CREATED,
            **_PAGE_NUM,
        },
        essential=True,
    ),
    _get("get_airwallex_financial_transaction", "One financial transaction", "/api/v1/financial_transactions" + _ID),
    _get(
        "list_airwallex_global_accounts",
        "Global accounts (receiving bank accounts), numbers masked",
        "/api/v1/global_accounts",
        query={
            "status": _p(_STR, "ACTIVE, INACTIVE or CLOSED"),
            "country_code": _p(_STR, "Country (ISO 3166-1 alpha-2)"),
            "nick_name": _p(_STR, "Nickname"),
            **_CREATED,
            **_CURSOR,
        },
        essential=True,
    ),
    _get("get_airwallex_global_account", "One global account, numbers masked", "/api/v1/global_accounts" + _ID),
    _get(
        "list_airwallex_global_account_transactions",
        "Transactions received on one global account",
        "/api/v1/global_accounts/{id}/transactions",
        query={**_CREATED, **_PAGE_NUM},
    ),
    _get(
        "list_airwallex_deposits",
        "Deposits received: amount, currency, status and settlement",
        "/api/v1/deposits",
        query={**_CREATED, **_PAGE_NUM},
    ),
    _get("get_airwallex_deposit", "One deposit", "/api/v1/deposits" + _ID),
    _get(
        "list_airwallex_transfers",
        "Transfers (payouts): amounts, currencies, beneficiary, status, transfer date",
        "/api/v1/transfers",
        query={
            "status": _p(_STR, "e.g. IN_APPROVAL, SCHEDULED, PROCESSING, SENT, PAID, FAILED, CANCELLED"),
            "transfer_currency": _p(_STR, "Currency the beneficiary receives"),
            "beneficiary_id": _p(_STR, "Beneficiary id"),
            "short_reference_id": _p(_STR, "Short reference shown in the web app"),
            "request_id": _p(_STR, "The request_id the transfer was created with"),
            **_CREATED,
            **_CURSOR,
        },
        essential=True,
    ),
    _get("get_airwallex_transfer", "One transfer with its status", "/api/v1/transfers" + _ID),
    _get(
        "list_airwallex_beneficiaries",
        "Beneficiaries (payees), bank details masked",
        "/api/v1/beneficiaries",
        query={
            "name": _p(_STR, "Beneficiary name"),
            "nick_name": _p(_STR, "Nickname"),
            "company_name": _p(_STR, "Company name"),
            "entity_type": _p(_STR, "COMPANY or PERSONAL"),
            "from_date": _p(_STR, "Created on or after (YYYY-MM-DD)"),
            "to_date": _p(_STR, "Created on or before (YYYY-MM-DD)"),
            **_PAGE_NUM,
        },
    ),
    _get("get_airwallex_beneficiary", "One beneficiary, bank details masked", "/api/v1/beneficiaries" + _ID),
    _get(
        "get_airwallex_fx_rate",
        "Current FX rate for a currency pair (indicative, not a quote)",
        "/api/v1/fx/rates/current",
        query={
            "buy_currency": _p(_STR, "Currency bought (ISO 4217)"),
            "sell_currency": _p(_STR, "Currency sold (ISO 4217)"),
            "buy_amount": _p({"type": "number"}, "Amount bought (give this or sell_amount)"),
            "sell_amount": _p({"type": "number"}, "Amount sold"),
            "conversion_date": _p(_STR, "Settlement date (YYYY-MM-DD)"),
        },
        required=("buy_currency", "sell_currency"),
        essential=True,
    ),
    _get("get_airwallex_fx_quote", "One FX quote", "/api/v1/fx/quotes/{quote_id}"),
    _get(
        "list_airwallex_conversions",
        "FX conversions: currencies, amounts, rate, status, conversion date",
        "/api/v1/fx/conversions",
        query={
            "status": _p(_STR, "SCHEDULED, SETTLED, OVERDUE or CANCELLED"),
            "buy_currency": _p(_STR, "Currency bought"),
            "sell_currency": _p(_STR, "Currency sold"),
            "request_id": _p(_STR, "The request_id the conversion was created with"),
            **_CREATED,
            **_PAGE_NUM,
        },
    ),
    _get("get_airwallex_conversion", "One conversion", "/api/v1/fx/conversions/{conversion_id}"),
    _get(
        "list_airwallex_cards",
        "Issued cards: status, cardholder, nickname, limits (numbers masked)",
        "/api/v1/issuing/cards",
        query={
            "card_status": _p(_STR, "PENDING, ACTIVE, INACTIVE, BLOCKED, LOST, STOLEN, CLOSED..."),
            "cardholder_id": _p(_STR, "Cardholder id"),
            "nick_name": _p(_STR, "Nickname"),
            **_CREATED,
            **_PAGE_NUM,
        },
    ),
    _get("get_airwallex_card", "One card (number masked)", "/api/v1/issuing/cards" + _ID),
    _get("get_airwallex_card_limits", "Remaining limits of one card", "/api/v1/issuing/cards/{id}/limits"),
    _get(
        "list_airwallex_cardholders",
        "Cardholders",
        "/api/v1/issuing/cardholders",
        query={"cardholder_status": _p(_STR, "PENDING, READY, DISABLED, INCOMPLETE"), **_PAGE_NUM},
    ),
    _get("get_airwallex_cardholder", "One cardholder", "/api/v1/issuing/cardholders" + _ID),
    _get(
        "list_airwallex_card_transactions",
        "Card transactions: merchant, amounts in transaction and billing currency, status",
        "/api/v1/issuing/transactions",
        query={
            "card_id": _p(_STR, "Card id"),
            "billing_currency": _p(_STR, "Billing currency"),
            "transaction_type": _p(_STR, "AUTHORIZATION, CLEARING, REFUND, REVERSAL, ORIGINAL_CREDIT"),
            **_CREATED,
            **_PAGE_NUM,
        },
    ),
    _get("get_airwallex_card_transaction", "One card transaction", "/api/v1/issuing/transactions" + _ID),
    _get(
        "list_airwallex_invoices",
        "Billing invoices (when Billing is enabled)",
        "/api/v1/billing/invoices",
        query={
            "status": _p(_STR, "DRAFT, FINALIZED, VOIDED"),
            "payment_status": _p(_STR, "UNPAID, PAID..."),
            "billing_customer_id": _p(_STR, "Billing customer id"),
            "subscription_id": _p(_STR, "Subscription id"),
            **_CREATED,
            **_CURSOR,
        },
    ),
    _get("get_airwallex_invoice", "One billing invoice", "/api/v1/billing/invoices" + _ID),
    _get(
        "list_airwallex_invoice_line_items",
        "Line items of one billing invoice",
        "/api/v1/billing/invoices/{id}/line_items",
        query=_CURSOR,
    ),
    _get(
        "list_airwallex_payment_intents",
        "Payment acceptance: payment intents (when Payments is enabled)",
        "/api/v1/pa/payment_intents",
        query={
            "status": _p(_STR, "e.g. SUCCEEDED, REQUIRES_PAYMENT_METHOD, CANCELLED"),
            "currency": _p(_STR, "Currency"),
            "merchant_order_id": _p(_STR, "Merchant order id"),
            **_CREATED,
            **_PAGE_NUM,
        },
    ),
    _get("get_airwallex_payment_intent", "One payment intent", "/api/v1/pa/payment_intents" + _ID),
    _get(
        "list_airwallex_settlements",
        "Payment acceptance settlements (when Payments is enabled)",
        "/api/v1/pa/financial/settlements",
        query={
            "currency": _p(_STR, "Settlement currency"),
            "status": _p(_STR, "PENDING, SETTLED..."),
            "from_settled_at": _p(_STR, "Settled at or after (ISO 8601)"),
            "to_settled_at": _p(_STR, "Settled before (ISO 8601)"),
            **_PAGE_NUM,
        },
        required=("currency", "status", "from_settled_at", "to_settled_at"),
    ),
    _get("get_airwallex_settlement", "One settlement", "/api/v1/pa/financial/settlements" + _ID),
    _get(
        "list_airwallex_financial_reports",
        "Financial reports already created, with their status",
        "/api/v1/finance/financial_reports",
        query=_PAGE_NUM,
    ),
    _get("get_airwallex_financial_report", "One financial report's status", "/api/v1/finance/financial_reports" + _ID),
    _get(
        "list_airwallex_payment_links",
        "Payment links (when Payments is enabled)",
        "/api/v1/pa/payment_links",
        query={
            "active": _p({"type": "boolean"}, "Active links only"),
            "reusable": _p({"type": "boolean"}, "Reusable links only"),
            **_CREATED,
            **_PAGE_NUM,
        },
    ),
    _get("get_airwallex_payment_link", "One payment link", "/api/v1/pa/payment_links" + _ID),
    _get("list_airwallex_webhooks", "Webhooks configured on the account", "/api/v1/webhooks", query=_CURSOR),
)

_REQUEST_ID = _p(_STR, "Idempotency key; a new UUID is used when omitted. Reuse it to retry safely")
_OBJ = {"type": "object"}
_BENEFICIARY = {
    "beneficiary": _p(_OBJ, "The beneficiary as the API takes it (entity_type, bank_details, address...)"),
    "transfer_methods": _p({"type": "array", "items": _STR}, "LOCAL and/or SWIFT"),
    "nickname": _p(_STR, "Nickname"),
}


def _write(
    name: str,
    summary: str,
    path: str,
    *,
    body: dict[str, dict[str, Any]] | None = None,
    required: tuple[str, ...] = (),
    fixed_body: dict[str, Any] | None = None,
    group: str,
) -> Operation:
    path_params = tuple(part[1:-1] for part in path.split("/") if part.startswith("{"))
    return Operation(
        name=name,
        summary=summary,
        method="POST",
        path=path,
        path_params=path_params,
        body=dict(body or {}),
        required=path_params + required,
        write=True,
        fixed_body=dict(fixed_body or {}),
        group=group,
    )


#: Every write: built always, asks a person on every call (always_ask), named
#: with a write verb so a read-only binding withholds it. ``group`` names the
#: family in the tool description (beneficiaries, transfers, fx, cards...).
WRITE_OPERATIONS: tuple[Operation, ...] = (
    _write(
        "create_airwallex_beneficiary",
        "Create a beneficiary (payee)",
        "/api/v1/beneficiaries/create",
        body=_BENEFICIARY,
        required=("beneficiary", "transfer_methods"),
        group="beneficiaries",
    ),
    _write(
        "update_airwallex_beneficiary",
        "Update a beneficiary's bank details or nickname",
        "/api/v1/beneficiaries/{id}/update",
        body=_BENEFICIARY,
        required=("beneficiary", "transfer_methods"),
        group="beneficiaries",
    ),
    # Checks the details and saves nothing: a read of the beneficiaries group.
    Operation(
        name="validate_airwallex_beneficiary",
        summary="Validate beneficiary details without saving them",
        method="POST",
        path="/api/v1/beneficiaries/validate",
        body=_BENEFICIARY,
        required=("beneficiary", "transfer_methods"),
        group="beneficiaries",
    ),
    _write(
        "delete_airwallex_beneficiary",
        "Delete a beneficiary",
        "/api/v1/beneficiaries/{id}/delete",
        group="beneficiaries",
    ),
    _write(
        "create_airwallex_transfer",
        "Create a transfer (payout) to a beneficiary",
        "/api/v1/transfers/create",
        body={
            "beneficiary_id": _p(_STR, "Existing beneficiary id"),
            "transfer_currency": _p(_STR, "Currency the beneficiary receives"),
            "transfer_amount": _p({"type": "number"}, "Amount the beneficiary receives (or source_amount)"),
            "source_currency": _p(_STR, "Currency debited"),
            "source_amount": _p({"type": "number"}, "Amount debited"),
            "transfer_method": _p(_STR, "LOCAL or SWIFT"),
            "transfer_date": _p(_STR, "YYYY-MM-DD"),
            "quote_id": _p(_STR, "A quote to lock the rate"),
            "reason": _p(_STR, "Transfer reason code"),
            "reference": _p(_STR, "Reference shown to the beneficiary"),
            "request_id": _REQUEST_ID,
        },
        required=("beneficiary_id", "transfer_currency", "reason", "reference"),
        group="transfers",
    ),
    _write(
        "cancel_airwallex_transfer",
        "Cancel a transfer not yet sent",
        "/api/v1/transfers/{id}/cancel",
        group="transfers",
    ),
    _write(
        "create_airwallex_fx_quote",
        "Lock an FX quote for a currency pair",
        "/api/v1/fx/quotes/create",
        body={
            "buy_currency": _p(_STR, "Currency bought"),
            "sell_currency": _p(_STR, "Currency sold"),
            "buy_amount": _p({"type": "number"}, "Amount bought (give this or sell_amount)"),
            "sell_amount": _p({"type": "number"}, "Amount sold"),
            "validity": {
                "type": "string",
                "enum": ["MIN_1", "MIN_15", "MIN_30", "HR_1", "HR_4", "HR_8", "HR_24"],
                "description": "How long the rate holds",
            },
            "conversion_date": _p(_STR, "YYYY-MM-DD"),
        },
        required=("buy_currency", "sell_currency", "validity"),
        group="fx",
    ),
    _write(
        "create_airwallex_conversion",
        "Book an FX conversion between two wallet currencies",
        "/api/v1/fx/conversions/create",
        body={
            "buy_currency": _p(_STR, "Currency bought"),
            "sell_currency": _p(_STR, "Currency sold"),
            "buy_amount": _p({"type": "number"}, "Amount bought (give this or sell_amount)"),
            "sell_amount": _p({"type": "number"}, "Amount sold"),
            "quote_id": _p(_STR, "A quote to lock the rate"),
            "conversion_date": _p(_STR, "YYYY-MM-DD"),
            "request_id": _REQUEST_ID,
        },
        required=("buy_currency", "sell_currency"),
        group="fx",
    ),
    _write(
        "freeze_airwallex_card",
        "Freeze a card (status INACTIVE)",
        "/api/v1/issuing/cards/{id}/update",
        fixed_body={"card_status": "INACTIVE"},
        group="cards",
    ),
    _write(
        "unfreeze_airwallex_card",
        "Unfreeze a card (status ACTIVE)",
        "/api/v1/issuing/cards/{id}/update",
        fixed_body={"card_status": "ACTIVE"},
        group="cards",
    ),
    _write(
        "update_airwallex_card",
        "Update a card's nickname or authorization controls (limits, merchant rules)",
        "/api/v1/issuing/cards/{id}/update",
        body={
            "nick_name": _p(_STR, "Nickname"),
            "authorization_controls": _p(_OBJ, "Limits and allowed merchants, as the API takes them"),
        },
        group="cards",
    ),
    _write(
        "create_airwallex_cardholder",
        "Create a cardholder",
        "/api/v1/issuing/cardholders/create",
        body={
            "type": _p(_STR, "INDIVIDUAL or DELEGATE"),
            "email": _p(_STR, "Email"),
            "individual": _p(_OBJ, "Name, date of birth, address, as the API takes them"),
            "mobile_number": _p(_STR, "Mobile number"),
            "postal_address": _p(_OBJ, "Postal address"),
        },
        required=("type", "email", "individual"),
        group="cards",
    ),
    _write(
        "update_airwallex_cardholder",
        "Update a cardholder",
        "/api/v1/issuing/cardholders/{id}/update",
        body={
            "individual": _p(_OBJ, "Name, address, as the API takes them"),
            "mobile_number": _p(_STR, "Mobile number"),
            "postal_address": _p(_OBJ, "Postal address"),
        },
        group="cards",
    ),
    _write(
        "create_airwallex_payment_link",
        "Create a payment link a customer pays",
        "/api/v1/pa/payment_links/create",
        body={
            "title": _p(_STR, "Title the payer sees"),
            "reusable": _p({"type": "boolean"}, "Reusable by several payers"),
            "amount": _p({"type": "number"}, "Amount (fixed-amount link)"),
            "currency": _p(_STR, "Currency"),
            "description": _p(_STR, "Description"),
            "reference": _p(_STR, "Your reference"),
            "expires_at": _p(_STR, "Expiry (ISO 8601)"),
        },
        required=("title", "reusable"),
        group="payments",
    ),
    _write(
        "deactivate_airwallex_payment_link",
        "Deactivate a payment link",
        "/api/v1/pa/payment_links/{id}/deactivate",
        group="payments",
    ),
    _write(
        "create_airwallex_invoice",
        "Create a draft billing invoice",
        "/api/v1/billing/invoices/create",
        body={
            "billing_customer_id": _p(_STR, "Billing customer id"),
            "currency": _p(_STR, "Currency"),
            "due_date": _p(_STR, "YYYY-MM-DD"),
            "memo": _p(_STR, "Memo"),
            "request_id": _REQUEST_ID,
        },
        required=("billing_customer_id", "currency"),
        group="billing",
    ),
    _write(
        "finalize_airwallex_invoice",
        "Finalize a draft billing invoice",
        "/api/v1/billing/invoices/{id}/finalize",
        group="billing",
    ),
    _write(
        "void_airwallex_invoice",
        "Void a billing invoice",
        "/api/v1/billing/invoices/{id}/void",
        group="billing",
    ),
)


def operations() -> tuple[Operation, ...]:
    """Every operation: the reads, then the writes (each asks a person)."""
    return READ_OPERATIONS + WRITE_OPERATIONS


# --- tools -------------------------------------------------------------------------------


def _schema(op: Operation) -> dict[str, Any]:
    properties: dict[str, Any] = {name: _p(_STR, f"The {name.replace('_', ' ')}") for name in op.path_params}
    properties.update(op.query)
    properties.update(op.body)
    properties["on_behalf_of"] = _ON_BEHALF_OF
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if op.required:
        schema["required"] = list(op.required)
    return schema


def _description(op: Operation) -> str:
    text = f"{op.summary} (Airwallex {op.method} {op.path})."
    if op.write:
        text += f" Write ({op.group}): asks a person on every call."
        if "request_id" in op.body:
            text += " Idempotent on request_id."
    elif op.group:
        text += f" Read ({op.group}): saves nothing."
    elif op.query:
        text += " Amounts come with their currency."
    return text


def _fill_path(op: Operation, kwargs: dict[str, Any]) -> tuple[str, str | None]:
    path = op.path
    for name in op.path_params:
        value = kwargs.get(name)
        if value is None or str(value).strip() == "":
            return "", f"Error: {name} is required."
        path = path.replace("{" + name + "}", seg(value))
    return path, None


def _make_handler(client: AirwallexClient, op: Operation) -> Callable[..., Awaitable[str]]:
    action = op.summary[0].lower() + op.summary[1:]

    async def handler(**kwargs: Any) -> str:
        path, err = _fill_path(op, kwargs)
        if err:
            return err
        for name in op.required:
            if name not in op.path_params and kwargs.get(name) in (None, "", []):
                return f"Error: {name} is required."
        on_behalf_of = kwargs.get("on_behalf_of") or None
        query = {name: kwargs[name] for name in op.query if name in kwargs}
        payload: dict[str, Any] | None = None
        if op.method != "GET":
            payload = {name: kwargs[name] for name in op.body if name in kwargs}
            payload.update(op.fixed_body)
            if "request_id" in op.body and not payload.get("request_id"):
                payload["request_id"] = str(uuid.uuid4())
        resp, err = await client.request(op.method, path, action, params=query, body=payload, on_behalf_of=on_behalf_of)
        if err or resp is None:
            return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
        title = f"{PROVIDER}: {op.summary}{_page_note(resp.data)}"
        if on_behalf_of:
            title += f" [account {on_behalf_of}]"
        if op.write and payload and payload.get("request_id"):
            title += f" [request_id {payload['request_id']}]"
        if resp.data is None and op.method != "GET":
            return f"{title}: done."
        return render(title, resp.data)

    return handler


_REPORT_TYPES = [
    "ACCOUNT_STATEMENT_REPORT",
    "BALANCE_ACTIVITY_REPORT",
    "TRANSACTION_RECON_REPORT",
    "SETTLEMENT_REPORT",
    "ONLINE_PAYMENTS_TRANSACTION_REPORT",
]
_REPORT_FORMATS = {
    "CSV": ("csv", "text/csv"),
    "EXCEL": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "PDF": ("pdf", "application/pdf"),
}


async def _download_report(
    client: AirwallexClient,
    report_id: str,
    report: dict[str, Any],
    on_behalf_of: str | None,
    persist_file_fn: Any,
) -> str | ToolResult:
    fmt = str(report.get("file_format") or "CSV").upper()
    extension, content_type = _REPORT_FORMATS.get(fmt, ("csv", "text/csv"))
    resp, err = await client.request(
        "GET",
        f"/api/v1/finance/financial_reports/{seg(report_id)}/content",
        "download the financial report",
        on_behalf_of=on_behalf_of,
        accept="*/*",
    )
    if err or resp is None:
        return err or f"Error: {PROVIDER} answered no report content."
    name = str(report.get("file_name") or f"airwallex-{str(report.get('type') or 'report').lower()}-{report_id}")
    if not name.lower().endswith(f".{extension}"):
        name = f"{name}.{extension}"
    period = f"{report.get('from_date') or '?'} to {report.get('to_date') or '?'}"
    details = [f"Airwallex {report.get('type') or 'financial report'}, {period}."]
    if on_behalf_of:
        details.append(f"Account {on_behalf_of}.")
    return await deliver_file(
        data=resp.content,
        filename=name,
        content_type=resp.content_type or content_type,
        source=PROVIDER,
        persist_file_fn=persist_file_fn,
        details=details,
    )


def _report_tools(
    client: AirwallexClient, executor: CallableToolExecutor, persist_file_fn: Any
) -> list[ToolDefinition]:
    async def export_report(**kwargs: Any) -> str | ToolResult:
        missing = [name for name in ("type", "from_date", "to_date") if not kwargs.get(name)]
        if missing:
            return f"Error: {', '.join(missing)} required."
        on_behalf_of = kwargs.get("on_behalf_of") or None
        body: dict[str, Any] = {
            "type": str(kwargs["type"]).upper(),
            "from_date": kwargs["from_date"],
            "to_date": kwargs["to_date"],
            "file_format": str(kwargs.get("file_format") or "CSV").upper(),
        }
        for name in ("currencies", "transaction_types", "time_zone"):
            if kwargs.get(name):
                body[name] = kwargs[name]
        resp, err = await client.request(
            "POST",
            "/api/v1/finance/financial_reports/create",
            "create the financial report",
            body=body,
            on_behalf_of=on_behalf_of,
        )
        if err or resp is None:
            return err or f"Error: {PROVIDER} answered nothing while creating the report."
        report = resp.data if isinstance(resp.data, dict) else {}
        report_id = str(report.get("id") or "")
        if not report_id:
            return render(f"{PROVIDER}: the report was not created", resp.data)
        for _ in range(REPORT_POLLS):
            status = str(report.get("status") or "").upper()
            if status == "COMPLETED":
                return await _download_report(client, report_id, report, on_behalf_of, persist_file_fn)
            if status == "FAILED":
                return render(f"Error: {PROVIDER} failed to generate report {report_id}", report)
            await asyncio.sleep(REPORT_POLL_SECONDS)
            got, err = await client.request(
                "GET",
                f"/api/v1/finance/financial_reports/{seg(report_id)}",
                "read the financial report status",
                on_behalf_of=on_behalf_of,
            )
            if err or got is None:
                return err or f"Error: {PROVIDER} answered nothing on report {report_id}."
            report = got.data if isinstance(got.data, dict) else report
        return (
            f"{PROVIDER}: report {report_id} is still {report.get('status') or 'PENDING'}; "
            f"call download_airwallex_financial_report with report_id {report_id} in a minute."
        )

    async def download_report(**kwargs: Any) -> str | ToolResult:
        report_id = str(kwargs.get("report_id") or "").strip()
        if not report_id:
            return "Error: report_id is required."
        on_behalf_of = kwargs.get("on_behalf_of") or None
        got, err = await client.request(
            "GET",
            f"/api/v1/finance/financial_reports/{seg(report_id)}",
            "read the financial report",
            on_behalf_of=on_behalf_of,
        )
        if err or got is None:
            return err or f"Error: {PROVIDER} answered nothing on report {report_id}."
        report = got.data if isinstance(got.data, dict) else {}
        if str(report.get("status") or "").upper() != "COMPLETED":
            return render(f"{PROVIDER}: report {report_id} is not ready", report)
        return await _download_report(client, report_id, report, on_behalf_of, persist_file_fn)

    executor.register("export_airwallex_financial_report", export_report)
    executor.register("download_airwallex_financial_report", download_report)
    return [
        ToolDefinition(
            name="export_airwallex_financial_report",
            description=(
                "Generate an Airwallex financial report (account statement, balance activity, transaction "
                "reconciliation, settlement) for a date range and deliver it as a file of the run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": _REPORT_TYPES, "description": "Report type"},
                    "from_date": _p(_STR, "First day (YYYY-MM-DD), inclusive"),
                    "to_date": _p(_STR, "Last day (YYYY-MM-DD), inclusive"),
                    "file_format": {"type": "string", "enum": list(_REPORT_FORMATS), "description": "Default CSV"},
                    "currencies": _p(
                        {"type": "array", "items": _STR}, "Currencies (required for ACCOUNT_STATEMENT_REPORT)"
                    ),
                    "transaction_types": _p({"type": "array", "items": _STR}, "Transaction types to keep"),
                    "time_zone": _p(_STR, "IANA time zone of the report dates"),
                    "on_behalf_of": _ON_BEHALF_OF,
                },
                "required": ["type", "from_date", "to_date"],
                "additionalProperties": False,
            },
            source=SOURCE,
            priority=0,
            # Generating a report changes nothing on the account.
            read_only=True,
        ),
        ToolDefinition(
            name="download_airwallex_financial_report",
            description="Download a completed Airwallex financial report by id as a file of the run.",
            input_schema={
                "type": "object",
                "properties": {"report_id": _p(_STR, "Report id"), "on_behalf_of": _ON_BEHALF_OF},
                "required": ["report_id"],
                "additionalProperties": False,
            },
            source=SOURCE,
            priority=1,
        ),
    ]


def build_operation_tools(
    client: AirwallexClient,
    executor: CallableToolExecutor,
    *,
    persist_file_fn: Any = None,
) -> list[ToolDefinition]:
    """A tool per operation of the table, plus the report export, handlers registered."""
    tools: list[ToolDefinition] = []
    for op in operations():
        executor.register(op.name, _make_handler(client, op))
        tools.append(
            ToolDefinition(
                name=op.name,
                description=_description(op),
                input_schema=_schema(op),
                source=SOURCE,
                priority=0 if op.essential else (2 if op.write else 1),
                always_ask=op.write,
            )
        )
    tools.extend(_report_tools(client, executor, persist_file_fn))
    return tools
