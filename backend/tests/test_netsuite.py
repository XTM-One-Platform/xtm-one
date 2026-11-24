"""NetSuite provider: the two auth modes, SuiteQL paging, records, finance tools and the write guards.

Every NetSuite answer is a recorded fixture served over ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token, build_tools_for_provider
from app.integrations.http import _APP_TOKEN_CACHE
from app.integrations.providers import netsuite
from app.tools.base import CallableToolExecutor
from tests.integration_file_fakes import call, fake_api

_ACCOUNT = "1234567_SB1"
_REST = "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest"
_TBA = f"{_ACCOUNT}||tba||ck||cs||tid||ts"

_SUBSIDIARIES = {
    "items": [
        {"links": [], "id": "1", "name": "Parent Co", "fullname": "Parent Co", "base_currency": "EUR"},
        {"links": [], "id": "2", "name": "US Inc", "fullname": "Parent Co : US Inc", "base_currency": "USD"},
    ],
    "hasMore": False,
    "totalResults": 2,
}


def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _route(request: httpx.Request) -> httpx.Response:
    url = urlsplit(str(request.url))
    if url.path.endswith("/auth/oauth2/v1/token"):
        return httpx.Response(200, json={"access_token": "m2m-access", "expires_in": 3600, "token_type": "bearer"})
    if url.path.endswith("/query/v1/suiteql"):
        q = json.loads(request.content)["q"]
        params = parse_qs(url.query)
        if "FROM subsidiary" in q:
            return httpx.Response(200, json=_SUBSIDIARIES)
        if "FROM fail_permission" in q:
            return httpx.Response(
                403,
                json={"o:errorDetails": [{"detail": "INSUFFICIENT_PERMISSION: You do not have permission"}]},
            )
        offset = int(params["offset"][0])
        limit = int(params["limit"][0])
        rows = [{"id": str(i), "tranid": f"T{i}"} for i in range(offset, min(offset + limit, 2500))]
        return httpx.Response(
            200, json={"items": rows, "hasMore": offset + limit < 2500, "totalResults": 2500, "count": len(rows)}
        )
    if url.path.endswith("/record/v1/vendorBill/42"):
        return httpx.Response(
            200,
            json={
                "links": [{"rel": "self"}],
                "id": "42",
                "tranId": "VB-42",
                "total": 120.5,
                "currency": {"id": "1", "refName": "EUR"},
            },
        )
    if url.path.endswith("/record/v1/vendorBill") and request.method == "GET":
        return httpx.Response(200, json={"items": [{"id": "42"}, {"id": "43"}], "hasMore": True, "totalResults": 9})
    if url.path.endswith("/record/v1/vendorBill") and request.method == "POST":
        return httpx.Response(204, headers={"Location": f"{_REST}/record/v1/vendorBill/77"})
    if url.path.endswith("/record/v1/journalEntry/5") and request.method == "PATCH":
        return httpx.Response(
            400,
            json={"o:errorDetails": [{"detail": "The accounting period is locked for this transaction"}]},
        )
    return httpx.Response(404, json={"title": f"no route {request.method} {url.path}"})


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _APP_TOKEN_CACHE.clear()
    yield
    _APP_TOKEN_CACHE.clear()


# ---------------------------------------------------------------------------
# Credentials and the two auth modes
# ---------------------------------------------------------------------------


def test_sandbox_account_ids_map_to_hosts_and_realms():
    assert netsuite.account_host("1234567_SB1") == "1234567-sb1"
    assert netsuite.account_realm("1234567-sb1") == "1234567_SB1"
    assert netsuite.rest_base("1234567") == "https://1234567.suitetalk.api.netsuite.com/services/rest"
    assert netsuite.restlet_url("1234567_SB1").startswith("https://1234567-sb1.restlets.api.netsuite.com/")


def test_the_compound_credential_is_assembled_per_mode_and_parsed_back():
    tba = netsuite.compound_credential(
        "ts",
        {"account_id": _ACCOUNT, "auth_mode": "tba", "consumer_key": "ck", "consumer_secret": "cs", "token_id": "tid"},
    )
    assert tba == _TBA
    assert netsuite.parse_token(tba)["token_secret"] == "ts"
    key = "-----BEGIN PRIVATE KEY-----\nab||cd\n-----END PRIVATE KEY-----"
    m2m = netsuite.compound_credential(key, {"account_id": _ACCOUNT, "oauth_client_id": "cid", "certificate_id": "kid"})
    fields = netsuite.parse_token(m2m)
    assert (fields["mode"], fields["client_id"], fields["certificate_id"], fields["scopes"]) == (
        "m2m",
        "cid",
        "kid",
        "rest_webservices",
    )
    assert fields["private_key"] == key  # the key keeps its ``||``
    assert netsuite.compound_credential(key, {"account_id": _ACCOUNT}) == ""
    assert netsuite.compound_credential("", {"account_id": _ACCOUNT, "auth_mode": "tba"}) == ""
    assert netsuite.build_tools("", CallableToolExecutor()) == []


def test_the_tba_header_is_a_valid_hmac_sha256_signature():
    fields = netsuite.parse_token(_TBA)
    url = f"{_REST}/query/v1/suiteql"
    header = netsuite.tba_authorization(
        "POST", url, {"limit": "10", "offset": "0"}, fields, nonce="n0nce", timestamp="1700000000"
    )
    parts = dict(p.split("=", 1) for p in header.removeprefix("OAuth ").split(", "))
    parts = {k: unquote(v.strip('"')) for k, v in parts.items()}
    assert parts["realm"] == "1234567_SB1"
    assert parts["oauth_signature_method"] == "HMAC-SHA256"
    params = {k: v for k, v in parts.items() if k.startswith("oauth_") and k != "oauth_signature"}
    params.update({"limit": "10", "offset": "0"})
    enc = netsuite._encode
    normalized = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(params.items()))
    base_string = f"POST&{enc(url)}&{enc(normalized)}"
    expected = base64.b64encode(hmac.new(b"cs&ts", base_string.encode(), hashlib.sha256).digest()).decode()
    assert parts["oauth_signature"] == expected


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_the_m2m_assertion_is_signed_with_the_certificate_key(kind):
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if kind == "rsa"
        else ec.generate_private_key(ec.SECP256R1())
    )
    fields = {
        "account_id": _ACCOUNT,
        "client_id": "cid",
        "certificate_id": "kid",
        "scopes": "rest_webservices,restlets",
        "private_key": _pem(key),
    }
    token = netsuite.client_assertion(fields, now=1_700_000_000)
    header = jwt.get_unverified_header(token)
    assert header["kid"] == "kid"
    assert header["alg"] == ("PS256" if kind == "rsa" else "ES256")
    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=[header["alg"]],
        audience=netsuite.token_url(_ACCOUNT),
        options={"verify_exp": False},
    )
    assert claims["iss"] == "cid"
    assert claims["scope"] == ["rest_webservices", "restlets"]
    assert claims["exp"] - claims["iat"] == 3600


@pytest.mark.asyncio
async def test_m2m_mints_one_token_and_sends_it_as_bearer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = netsuite.compound_credential(
        _pem(key), {"account_id": _ACCOUNT, "oauth_client_id": "cid", "certificate_id": "kid"}
    )
    executor = CallableToolExecutor()
    netsuite.build_tools(token, executor)
    with fake_api(_route) as sent:
        first = await call(executor, "list_netsuite_subsidiaries")
        await call(executor, "list_netsuite_subsidiaries")
    grants = [r for r in sent if r.url.path.endswith("/token")]
    assert len(grants) == 1  # cached for the second call
    form = parse_qs(grants[0].content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_assertion_type"] == ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"]
    queries = [r for r in sent if r.url.path.endswith("/suiteql")]
    assert all(r.headers["Authorization"] == "Bearer m2m-access" for r in queries)
    assert queries[0].headers["Prefer"] == "transient"
    assert "US Inc" in first.content and "USD" in first.content
    assert "links" not in first.content


@pytest.mark.asyncio
async def test_tba_signs_every_request_and_no_secret_reaches_a_result():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        result = await call(executor, "list_netsuite_subsidiaries")
    assert sent[0].headers["Authorization"].startswith('OAuth realm="1234567_SB1"')
    assert 'oauth_token="tid"' in sent[0].headers["Authorization"]
    assert "cs" not in sent[0].headers["Authorization"].split("oauth_signature")[0]
    assert "ts" not in result.content.split()


# ---------------------------------------------------------------------------
# SuiteQL, records, finance tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suiteql_pages_up_to_max_rows_and_says_more_remain():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        result = await call(executor, "run_netsuite_suiteql", query="SELECT id, tranid FROM transaction", max_rows=1500)
    pages = [parse_qs(urlsplit(str(r.url)).query) for r in sent]
    assert [(p["offset"][0], p["limit"][0]) for p in pages] == [("0", "1000"), ("1000", "500")]
    assert "1500 row(s) of 2500" in result.content
    assert "more rows remain" in result.content


@pytest.mark.asyncio
async def test_suiteql_refuses_dml():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        dml = await call(executor, "run_netsuite_suiteql", query="DELETE FROM transaction")
    assert "SELECT" in dml.content
    assert sent == []


@pytest.mark.asyncio
async def test_a_missing_permission_is_named_with_the_role_hint():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route):
        result = await call(executor, "run_netsuite_suiteql", query="SELECT 1 FROM fail_permission")
    assert "role of this integration lacks a permission" in result.content


@pytest.mark.asyncio
async def test_record_get_and_list():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route):
        record = await call(executor, "get_netsuite_record", record_type="vendorBill", record_id="42")
        listing = await call(executor, "list_netsuite_records", record_type="vendorBill")
    assert "VB-42" in record.content and "EUR" in record.content and "links" not in record.content
    assert "2 vendorBill record(s) of 9 (more: raise offset): 42, 43" in listing.content


@pytest.mark.asyncio
async def test_filters_are_validated_before_any_sql_is_sent():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        bad_id = await call(
            executor, "list_netsuite_transactions", transaction_type="invoice", subsidiary_id="1 OR 1=1"
        )
        bad_date = await call(
            executor, "list_netsuite_transactions", transaction_type="invoice", from_date="2026-13-01"
        )
        bad_type = await call(executor, "list_netsuite_transactions", transaction_type="payroll")
    assert "internal id" in bad_id.content
    assert "date" in bad_date.content
    assert "transaction_type is one of" in bad_type.content
    assert sent == []


@pytest.mark.asyncio
async def test_transactions_and_consolidated_statements_build_the_expected_suiteql():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        await call(
            executor,
            "list_netsuite_transactions",
            transaction_type="vendor_bill",
            subsidiary_id="2",
            period_id="31",
            min_amount=1000,
        )
        await call(executor, "get_netsuite_trial_balance", subsidiary_id="1", period_id="31", consolidated=True)
    tx_sql = json.loads(sent[0].content)["q"]
    assert "t.type = 'VendBill'" in tx_sql and "tl.subsidiary = 2" in tx_sql and "t.postingperiod = 31" in tx_sql
    assert "BUILTIN.DF(t.currency) AS currency" in tx_sql and "ABS(t.foreigntotal) >= 1000.0" in tx_sql
    tb_sql = json.loads(sent[-1].content)["q"]
    assert "BUILTIN.CONSOLIDATE(tal.amount, 'LEDGER', 'DEFAULT', 'DEFAULT', 1, 31, 'DEFAULT')" in tb_sql
    assert "ab.isprimary = 'T'" in tb_sql


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transactions_and_master_records_go_through_their_own_tools():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        created = await call(
            executor,
            "create_netsuite_transaction",
            record_type="vendorBill",
            body={"entity": {"id": "12"}, "subsidiary": {"id": "2"}},
        )
        wrong_tool = await call(executor, "create_netsuite_record", record_type="vendorBill", body={"x": 1})
        empty = await call(executor, "create_netsuite_transaction", record_type="vendorBill", body={})
    assert "vendorBill 77 created" in created.content
    assert "create_netsuite_transaction" in wrong_tool.content
    assert "JSON object" in empty.content
    assert len(sent) == 1 and sent[0].method == "POST"


@pytest.mark.asyncio
async def test_bank_details_are_read_and_written_on_the_entity_record():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        await call(executor, "get_netsuite_entity_bank_details", entity_id="12")
        await call(executor, "set_netsuite_entity_bank_details", entity_id="12", body={"custrecord_x": "y"})
    assert (
        "FROM customrecord_2663_entity_bank_details WHERE custrecord_2663_parent_vendor = 12"
        in json.loads(sent[0].content)["q"]
    )
    created = json.loads(sent[1].content)
    assert created == {"custrecord_2663_parent_vendor": {"id": "12"}, "custrecord_x": "y"}
    assert sent[1].url.path.endswith("/record/v1/customrecord_2663_entity_bank_details")


@pytest.mark.asyncio
async def test_generic_tools_refuse_bank_detail_tables_and_fields():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        answers = [
            await call(executor, "run_netsuite_suiteql", query="SELECT * FROM CUSTOMRECORD_2663_ENTITY_BANK_DETAILS"),
            await call(executor, "run_netsuite_suiteql", query="SELECT v.id, b.custrecord_2663_acct_num FROM vendor v"),
            await call(executor, "describe_netsuite_records", record_type="customrecord_2663_entity_bank_details"),
            await call(
                executor, "get_netsuite_record", record_type="customrecord_2663_entity_bank_details", record_id="1"
            ),
            await call(
                executor,
                "get_netsuite_record",
                record_type="vendor",
                record_id="12",
                fields="custrecord_2663_acct_num",
            ),
            await call(executor, "list_netsuite_records", record_type="customrecord_2663_entity_bank_details"),
        ]
    assert sent == []
    assert all("set_netsuite_entity_bank_details only" in a.content for a in answers)


@pytest.mark.asyncio
async def test_period_status_patches_the_accounting_period():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        none = await call(executor, "set_netsuite_period_status", period_id="31")
        await call(executor, "set_netsuite_period_status", period_id="31", closed=True, all_locked=True)
    assert "at least one" in none.content
    assert sent[0].method == "PATCH" and sent[0].url.path.endswith("/record/v1/accountingPeriod/31")
    assert json.loads(sent[0].content) == {"closed": True, "allLocked": True}


@pytest.mark.asyncio
async def test_saved_searches_and_files_need_the_companion_restlet():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        result = await call(executor, "run_netsuite_saved_search", search_id="customsearch_ap")
    assert "companion RESTlet" in result.content and sent == []

    token = netsuite.compound_credential(
        "ts",
        {
            "account_id": _ACCOUNT,
            "auth_mode": "tba",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "companion_script_id": "customscript_xtm",
            "companion_deploy_id": "1",
        },
    )
    assert netsuite.parse_token(token)["companion"] == "customscript_xtm:1"
    executor = CallableToolExecutor()
    netsuite.build_tools(token, executor)

    def companion(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "1234567-sb1.restlets.api.netsuite.com"
        assert request.url.params["script"] == "customscript_xtm"
        return httpx.Response(200, json={"rows": [{"entity": "Acme", "amount": "10"}], "more": False})

    with fake_api(companion):
        rows = await call(executor, "run_netsuite_saved_search", search_id="customsearch_ap")
    assert "1 row(s)" in rows.content and "Acme" in rows.content


@pytest.mark.asyncio
async def test_a_locked_period_is_explained():
    executor = CallableToolExecutor()
    netsuite.build_tools(_TBA, executor)
    with fake_api(_route) as sent:
        result = await call(
            executor, "update_netsuite_transaction", record_type="journalEntry", record_id="5", body={"memo": "x"}
        )
    assert "closed or locked" in result.content
    assert len(sent) == 1  # a write is never retried


# ---------------------------------------------------------------------------
# Catalog, read/write classification, Test Connection
# ---------------------------------------------------------------------------


def test_the_catalog_lists_every_tool_with_its_type():
    entry = PROVIDER_CATALOG["netsuite"]
    assert entry["integration_category"] == "business_operations"
    assert {"account_id", "auth_mode", "oauth_client_id", "certificate_id"} <= set(entry["extra_fields"])
    assert any("REST Web Services" in p["scope"] for p in entry["setup_guide"]["permissions"])
    tools = {t.name: t.tool_type for t in build_tools_for_provider("netsuite")}
    writes = {name for name, kind in tools.items() if kind == "write"}
    assert writes == {
        "create_netsuite_record",
        "update_netsuite_record",
        "create_netsuite_transaction",
        "update_netsuite_transaction",
        "set_netsuite_entity_bank_details",
        "set_netsuite_period_status",
        "attach_netsuite_file",
        "call_netsuite_restlet",
    }
    assert tools["run_netsuite_suiteql"] == "read"
    assert tools["get_netsuite_entity_bank_details"] == "read"
    assert len(tools) == 30
    asks = {d.name for d in netsuite._definitions() if d.always_ask}
    assert asks == {
        "create_netsuite_transaction",
        "update_netsuite_transaction",
        "set_netsuite_entity_bank_details",
        "set_netsuite_period_status",
        "call_netsuite_restlet",
    }
    assert build_dummy_token("netsuite").startswith("1234567||tba||")


def test_a_restlet_get_is_a_read_call():
    defs = {d.name: d for d in netsuite._definitions()}
    restlet = defs["call_netsuite_restlet"]
    assert restlet.read_only_call({"method": "GET"}) is True
    assert restlet.read_only_call({"method": "POST"}) is False


@pytest.mark.asyncio
async def test_validate_connection_names_the_account_and_the_mode():
    with fake_api(_route):
        ok, message = await netsuite.validate_connection(_TBA)
    assert ok
    assert "1234567_SB1" in message and "Token-Based Authentication" in message and "2 row(s)" in message
    with fake_api(lambda r: httpx.Response(401, json={"title": "Invalid login attempt."})):
        ok, message = await netsuite.validate_connection(_TBA)
    assert not ok
