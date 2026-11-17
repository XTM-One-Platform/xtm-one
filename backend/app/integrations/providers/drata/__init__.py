"""Drata integration — full coverage of the Drata Public API (v2 + v1 external-evidence, identity and retest surfaces).

The Drata Public API v2 exposes a comprehensive Governance, Risk &
Compliance (GRC) surface across Compliance, Policies, Controls, Risks,
Personnel, Vendors, Evidence, Assets, Audits, Frameworks, Monitoring
Tests, and more. This provider implements **list + get + create + update +
delete** for the resource families that have stable v2 endpoints, so an
agent can fully manage a Drata account end-to-end.

The provider is organized as a per-domain package (``_common`` + company,
access, policies, frameworks, audits, controls, risks, personnel, vendors,
assets, evidence, monitoring, user_identities, account), mirroring ``providers/opencti/``;
``build_tools`` wires every domain's ``register()`` together.

Docs: https://developers.drata.com/openapi/reference/v2/overview/
      https://developers.drata.com/openapi/reference/v1/tag/Controls/ (external evidence)
      https://developers.drata.com/openapi/reference/v1/tag/User-Identities/ (Version Control identities)

Implementation notes:

* All endpoints use **cursor-based pagination** (``pagination.cursor``)
  via the shared ``_paginated_fetch`` helper.
* The Drata ``expand[]`` query parameter is used to inline the most
  useful related collections in a single round trip (owners, controls,
  categories, customFields, …).
* Region routing (``us`` / ``eu`` / ``apac``) is resolved from the
  integration's ``extra_config.region`` and prepended to the bearer token
  via the existing ``base_url||api_key`` compound-token mechanism.
* All requests go through :func:`safe_request` so errors (401 / 403 /
  404 / 422 / 429 / 5xx) are formatted for the LLM rather than raising.
* Evidence Library items can carry a **real artifact**: ``create_drata_evidence``
  / ``update_drata_evidence`` render agent-authored Markdown / HTML to a real
  PDF (or DOCX) server-side and upload it via ``multipart/form-data`` (binary
  file) or attach an external ``url`` artifact via JSON — mirroring OpenCTI's
  ``upload_opencti_file``. LLMs cannot emit valid PDF binary inline, so they
  author the *content* and we perform the conversion. They can also forward a
  file the user uploaded to the conversation **byte-for-byte** via
  ``source_file_id`` (resolved from the run's ``FileContext``), so an attached
  PDF/document/image is filed as-is instead of re-authored.
* Evidence **verification** is first-class: ``create_drata_evidence`` /
  ``update_drata_evidence`` echo the new artifact ``version_id`` straight from
  the write response (deterministic read-your-write, immune to read caching),
  and ``get_drata_evidence_version`` reads a single artifact version's file name
  + download URL via the v2 ``…/versions/{versionId}`` endpoint so an agent can
  confirm exactly which file is filed.
* **Control scope** is surfaced and controllable: a control's in-scope /
  out-of-scope status is the v2 ``archivedAt`` field (out of scope == archived),
  filterable on the list via ``isArchived`` and shown on every control row +
  in the detail view. ``set_drata_control_scope`` drives the Perform Control
  Action endpoint (``markInScope`` / ``markOutOfScope``) — the API behind the
  Controls "Status: In scope / Out of scope" filter — so an agent can read AND
  change applicability instead of tracking it out-of-band.
* **Control external evidence** is a distinct object family from the v2 Evidence
  Library — records mapped directly to a control, managed on the **v1** API. The
  provider exposes ``list_drata_control_external_evidence`` /
  ``get_drata_external_evidence_download`` / ``upload_drata_control_external_evidence`` /
  ``delete_drata_control_external_evidence``, and ``get_drata_control`` surfaces the
  valid/invalid external-evidence IDs (not just counts). This is the cleanup path
  for evidence left dangling on controls after an Evidence Library purge: the two
  are different object types, so deleting library items never removes these.
  The v1 host is derived from the configured v2 base URL via ``to_v1_base_url``.
* **Risk management** mirrors the full v2 surface so an agent can run the
  programme end-to-end: Risk Registers (CRUD), Risks (CRUD + insights), the
  Risk Library (list / get / bulk-copy standard risks), Risk Notes (CRUD), and
  Risk Documents (list / get / upload / delete). Risk owners, reviewers,
  categories and linked controls are reassignable on both create and update via
  CSV ID fields — ``update_drata_risk`` REPLACES each set with the IDs passed,
  so a stale/departed owner is reassigned by passing the desired owner list
  (ISO 27001 6.1 / SOC 2 CC3.x risk-ownership upkeep).
* **Vendor security reviews** cover the full internal-assessment workflow so an
  agent can run a SOC 2 (Type 1 / Type 2) report review end-to-end without a
  human: create a review (optionally **atomically uploading the SOC report in the
  same call** via ``create_drata_vendor_security_review_with_file`` →
  ``/security-reviews/with-file``), fill the reviewer-side SOC assessment form via
  ``update_drata_vendor_security_review`` (``socForm`` — reviewer info, compliance
  scope, report opinion, findings, end-user controls, services & locations, CPA
  firm, subservice orgs), attach SOC reports / questionnaires (vendor documents +
  security questionnaires), and finalize/reopen via the review actions endpoint.
  ``delete_drata_vendor_security_review`` attempts the delete and, because Drata's
  Public API v2 currently exposes no delete route for reviews, degrades to an
  explicit, honest message (finalize/reopen via the actions endpoint, or remove it
  in the Drata UI) instead of silently failing or improvising a workaround. The
  separate vendor *questionnaires* family (sent to the vendor by email) is wired
  to the correct ``/vendors/{id}/questionnaires`` endpoints — distinct from the
  reviews you fill internally.
* **Vendor security owner**: ``create_drata_vendor`` / ``update_drata_vendor``
  accept ``user_id`` — the Drata user ID of the person responsible for the
  vendor's compliance (the "security owner") — so an agent can (re)assign a
  departed/stale owner. The owner also becomes the default review requester.
* **Personnel compliance evidence** (User Documents) is writable via
  ``upload_drata_user_document`` (+ list / get / delete). This is the only API
  path for **personnel offboarding evidence**: filing an artifact with
  ``doc_type='OFFBOARDING_EVIDENCE'`` against ``POST /users/{userId}/documents``
  satisfies a leaver's ``OFFBOARDING`` personnel compliance check — a DISTINCT
  surface from the workspace Evidence Library (an offboarding PDF filed there
  will not clear the check). It reuses the evidence artifact pipeline
  (render Markdown/HTML → PDF/DOCX, base64, or forward a user-uploaded file
  byte-for-byte) but enforces the narrower User Document file-type set, and
  ``userId`` accepts an ``email:<address>`` form so leavers reconciled from an
  HRIS can be addressed by email instead of numeric ID.
* **Version Control user identities** (v1 API only) are manageable so an agent
  can clear the Version Control monitoring tests (authorized / unique accounts,
  authorized code changes, restricted production changes) without the UI:
  ``list_drata_version_control_identities`` (compact projection, filterable to
  the ``not_linked`` ones), ``link_drata_version_control_identity`` (``userId``
  is the Drata USER id, not a personnel id),
  ``toggle_drata_version_control_identity`` ("Write access" / "Merge to default
  branch") and ``set_drata_version_control_service_account``. A fixed test is
  re-run at once with ``retest_drata_monitoring_test`` (the v1 autopilot
  retest) instead of waiting for the next scheduled run.
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import parse_compound_token
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    DEFAULT_BASE_URL,
    _headers,
    _upload_headers,
    resolve_base_url,
    resolve_base_url_v1,
)
from .access import register as register_access
from .account import register as register_account
from .assets import register as register_assets
from .audits import register as register_audits
from .company import register as register_company
from .controls import register as register_controls
from .evidence import register as register_evidence
from .frameworks import register as register_frameworks
from .monitoring import register as register_monitoring
from .personnel import register as register_personnel
from .policies import register as register_policies
from .risks import register as register_risks
from .user_identities import register as register_user_identities
from .vendors import register as register_vendors

__all__ = ["build_tools", "resolve_base_url", "resolve_base_url_v1"]


def build_tools(token: str, executor: CallableToolExecutor, **_kwargs: Any) -> list[ToolDefinition]:
    base_url, token = parse_compound_token(token, 2, defaults=(DEFAULT_BASE_URL, ""))
    if not base_url:
        base_url = DEFAULT_BASE_URL
    headers = _headers(token)
    upload_headers = _upload_headers(token)
    persist_file_fn = _kwargs.get("persist_file_fn")

    tools: list[ToolDefinition] = []
    tools += register_company(base_url, headers, upload_headers, executor)
    tools += register_access(base_url, headers, upload_headers, executor)
    tools += register_policies(base_url, headers, upload_headers, executor)
    tools += register_frameworks(base_url, headers, upload_headers, executor)
    tools += register_audits(base_url, headers, upload_headers, executor)
    tools += register_controls(base_url, headers, upload_headers, executor)
    tools += register_risks(base_url, headers, upload_headers, executor)
    tools += register_personnel(base_url, headers, upload_headers, executor)
    tools += register_vendors(base_url, headers, upload_headers, executor)
    tools += register_assets(base_url, headers, upload_headers, executor)
    tools += register_evidence(
        base_url, headers, upload_headers, executor, persist_file_fn=persist_file_fn
    )
    tools += register_monitoring(base_url, headers, upload_headers, executor)
    tools += register_user_identities(base_url, headers, upload_headers, executor)
    tools += register_account(base_url, headers, upload_headers, executor)
    return tools
