# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Read-classified platform tools, exempt from human approval by default.

A registered Suite platform contributes its whole API surface as tools — 256 of
them across OpenCTI, OpenAEV and OpenGRC — and roughly half cannot change
anything. Gating those buys nothing: an agent that pauses before reading a
report teaches an operator to click "always allow" on whatever is in front of
them, which is how the genuinely consequential calls stop being read.

So membership here is **derived, not enumerated**: a platform tool is exempt if
``infer_tool_type`` classifies it ``read``. Deriving is what keeps the list
honest across releases — a tool added next quarter arrives already classified,
and a hand-maintained list of names would be wrong the day it is written.

Two things bound the trust placed in that classifier:

* It is the same one behind the "Read" badge in the integration catalog:
  token-based, and **fail-safe to ``write``** for anything it does not
  recognise. An unknown verb gates; it never silently exempts.
* Its failure mode is a tool that keeps its name while gaining a mutating
  parameter, which no name-based check can see. :data:`HAND_GATED` is where a
  human overrides the classifier, and the golden-file test in
  ``tests/test_hitl_whitelist_platform.py`` is what forces every future change
  to this set through review.

Deliberately NOT here: the ~2,350 tools of third-party integrations. A platform
read runs against the user's own per-user credential, so it can return nothing
the user could not fetch themselves. "Read someone's mailbox" is a different
argument and is not being made by this module.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Integration providers that are Suite platforms rather than third-party
#: connections. ``openbas`` is absent on purpose: it is a *registration*
#: identifier, never a provider key — ``hitl_whitelist._platform_providers``
#: resolves it to ``openaev`` before anything reaches here.
PLATFORM_PROVIDERS = frozenset({"opencti", "openaev", "opencrq"})

#: Tools the classifier calls ``read`` that a human decided should still ask.
#:
#: The override the module's own docstring promises. Each entry is a judgement
#: about consequence rather than about mutation — every one of these really is
#: a read. Reviewed as data: adding a name here re-gates a tool on every
#: deployment, and removing one exempts it everywhere.
#:
#: **Deliberately not here: identity and RBAC reads.** Four of them were —
#: OpenCRQ's platform RBAC overview, one user's effective permissions, the
#: tenant's members, its service accounts — on the grounds that "who can do
#: what" deserves a human even though it changes nothing. That set was
#: incoherent and bought no protection: the summary asked while every
#: constituent part stayed exempt. ``list_opencrq_users`` returns platform
#: users *with email*, ``get_opencrq_user`` their groups and roles,
#: ``get_opencrq_group`` its members, ``get_opencrq_role`` its capabilities and
#: assigned groups, and ``list_opencti_capabilities`` the same for OpenCTI. An
#: agent blocked on the overview assembled the same picture from four exempt
#: calls.
#:
#: Made consistent by dropping the four rather than extending to the family:
#: a read is a read, and an operator who does not want identity read unattended
#: re-gates those tools on the whitelist screen — a real control, unlike a
#: half-enforced one the product only appears to offer. Resist re-adding a
#: single name here without the whole family; that is how the inconsistency
#: arose.
#:
#: The same holds for an integration's directory reads, which the derived
#: layer exempts (``hitl_whitelist_derived``): ``get_entra_user`` /
#: ``list_entra_users`` and ``get_letsignit_user`` return a person's
#: directory attributes and change nothing, so they stay exempt with their
#: family. What a company-wide directory key can *change* is what asks:
#: Letsignit's user writes declare ``ToolDefinition.always_ask``, which no
#: rule, standing approval or switch clears.
#:
#: The download family is here in full for exactly that reason. The recorded
#: reason for ``download_opencti_file`` never was OpenCTI-specific, and
#: ``download_`` is a leading read verb, so the other eight classified as
#: reads and ran unattended: a prompt-injected agent pulled an arbitrary
#: SharePoint or ServiceNow attachment into context with no prompt while the
#: same action against OpenCTI paused. Every read-classified ``download_`` tool
#: a provider declares belongs here, which
#: ``test_every_read_classified_download_tool_is_hand_gated`` enforces, and so
#: does a tool that downloads under another verb
#: (``get_defender_investigation_package`` stores the ZIP it fetches).
_DOWNLOAD_REASON = (
    "Pulls a file's bytes into the model's context. Reading platform data "
    "the user can already see is one thing; loading an arbitrary "
    "third-party document nobody has looked at is another."
)

HAND_GATED: dict[str, str] = {
    "download_opencti_file": _DOWNLOAD_REASON,
    "download_airwallex_financial_report": _DOWNLOAD_REASON,
    "download_anyrun_analysis_file": _DOWNLOAD_REASON,
    "download_ashby_candidate_file": _DOWNLOAD_REASON,
    "download_ashby_interview_transcript": _DOWNLOAD_REASON,
    "download_bluesky_media": _DOWNLOAD_REASON,
    "download_concord_agreement": _DOWNLOAD_REASON,
    "download_concord_attachment": _DOWNLOAD_REASON,
    "download_crowdstrike_intel_report": _DOWNLOAD_REASON,
    "download_crowdstrike_ngsiem_lookup_file": _DOWNLOAD_REASON,
    "download_crowdstrike_sample": _DOWNLOAD_REASON,
    "download_crowdstrike_sandbox_artifact": _DOWNLOAD_REASON,
    "download_databricks_file": _DOWNLOAD_REASON,
    "download_dealhub_products_excel": _DOWNLOAD_REASON,
    "download_dealhub_quote_document": _DOWNLOAD_REASON,
    "download_docusign_document": _DOWNLOAD_REASON,
    "download_doppel_alert_screenshot": _DOWNLOAD_REASON,
    "download_drata_file": _DOWNLOAD_REASON,
    "download_facebook_media": _DOWNLOAD_REASON,
    "download_figma_image": _DOWNLOAD_REASON,
    "download_flashpoint_media": _DOWNLOAD_REASON,
    "download_github_file": _DOWNLOAD_REASON,
    "download_github_repository_archive": _DOWNLOAD_REASON,
    "download_github_release_asset": _DOWNLOAD_REASON,
    "download_github_workflow_artifact": _DOWNLOAD_REASON,
    "download_google_chat_attachment": _DOWNLOAD_REASON,
    "download_google_drive_file": _DOWNLOAD_REASON,
    "download_gti_dtm_alert_attachment": _DOWNLOAD_REASON,
    "download_gti_file_sample": _DOWNLOAD_REASON,
    "download_gti_report": _DOWNLOAD_REASON,
    "download_hackerone_report_pdf": _DOWNLOAD_REASON,
    "download_hibob_document": _DOWNLOAD_REASON,
    "download_hibob_report": _DOWNLOAD_REASON,
    "download_hubspot_file": _DOWNLOAD_REASON,
    "download_jira_attachment": _DOWNLOAD_REASON,
    "download_linkedin_media": _DOWNLOAD_REASON,
    "download_microsoft_channel_message_image": _DOWNLOAD_REASON,
    "download_microsoft_chat_message_image": _DOWNLOAD_REASON,
    "download_microsoft_file": _DOWNLOAD_REASON,
    "download_microsoft_meeting_recording": _DOWNLOAD_REASON,
    "download_netsuite_file": _DOWNLOAD_REASON,
    "download_notion_file": _DOWNLOAD_REASON,
    "download_odoo_attachments": _DOWNLOAD_REASON,
    "download_onepassword_file": _DOWNLOAD_REASON,
    "download_pennylane_invoice_pdf": _DOWNLOAD_REASON,
    "download_perk_invoice_pdf": _DOWNLOAD_REASON,
    "download_rydoo_receipt": _DOWNLOAD_REASON,
    "download_servicenow_attachment": _DOWNLOAD_REASON,
    "download_slack_file": _DOWNLOAD_REASON,
    "download_spendesk_payable_attachment": _DOWNLOAD_REASON,
    "download_spendesk_purchase_order_document": _DOWNLOAD_REASON,
    "download_tanium_file_evidence": _DOWNLOAD_REASON,
    "download_threatq_export": _DOWNLOAD_REASON,
    "download_threatq_file": _DOWNLOAD_REASON,
    "download_whatsapp_media": _DOWNLOAD_REASON,
    "download_wordsmith_file": _DOWNLOAD_REASON,
    "download_x_media": _DOWNLOAD_REASON,
    "download_xdr_script": _DOWNLOAD_REASON,
    "download_xtm_hub_resource": _DOWNLOAD_REASON,
    "download_youtrack_attachment": _DOWNLOAD_REASON,
    "get_defender_investigation_package": _DOWNLOAD_REASON,
}


#: Memoised result of :func:`_derive_platform_read_tools`, and **only ever a
#: complete one**. A partial pass is returned to its caller but not stored, so
#: the next call retries instead of serving a provider's failure for the life of
#: the process.
_READ_TOOLS_CACHE: dict[str, frozenset[str]] | None = None


def platform_read_tools() -> dict[str, frozenset[str]]:
    """``{provider: read-classified tool names}`` for the Suite platforms.

    Computed once per process, and **only a complete pass is memoised**.
    Caching a failed one was the defect: an `lru_cache` over the whole dict
    meant a provider whose builder raised gated every one of its derived reads
    until the pod restarted, decided by whichever request warmed the cache
    first. The imports below are deferred because they are fragile, which is
    what makes a startup-time failure plausible rather than theoretical.

    A provider's tool set changing under a running process (a Suite upgrade
    behind the same pod) is still stale until restart, and that is intended:
    ``provider_of`` reads this same answer and feeds the admin screen, so the
    two must not disagree mid-process.
    """
    global _READ_TOOLS_CACHE
    if _READ_TOOLS_CACHE is not None:
        return _READ_TOOLS_CACHE
    derived, complete = _derive_platform_read_tools()
    if complete:
        _READ_TOOLS_CACHE = derived
    return derived


def _derive_platform_read_tools() -> tuple[dict[str, frozenset[str]], bool]:
    """One introspection pass, as ``(by_provider, every_builder_succeeded)``.

    The builders are called with a dummy credential purely to harvest
    ``ToolDefinition`` metadata — the same trick the integration catalog uses,
    sharing its token helper so a provider that validates its token format does
    not early-return an empty list.

    A provider whose builder raises contributes **nothing** rather than a
    partial list. Half a tool set would mean half these tools silently start
    asking for approval again, which reads as a product bug rather than as the
    import failure it is — but the pass is reported incomplete so the answer is
    not frozen for the process.
    """
    # Imported here rather than at module scope: the classifier and the dummy
    # token live in the API layer, and a service importing an API module at
    # import time invites a cycle (``agent_loop`` resolves the same function
    # the same way, for the same reason).
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.services.tool_classification import infer_tool_type
    from app.tools.base import CallableToolExecutor

    derived: dict[str, frozenset[str]] = {}
    complete = True
    for provider in sorted(PLATFORM_PROVIDERS):
        builder = PROVIDER_TOOLS.get(provider)
        if builder is None:  # pragma: no cover - a provider we ship, removed
            logger.warning("No tool builder for platform provider %s", provider)
            # Key set in every branch: ``read_tools_for`` uses ``.get`` and is
            # safe either way, but an absent key invites a future ``[provider]``
            # lookup to raise. A missing builder is a shipped fact, not a
            # failure, so it does not spoil the pass.
            derived[provider] = frozenset()
            continue
        try:
            definitions = builder(
                build_dummy_token(provider),
                CallableToolExecutor(),
                refresh_fn=lambda: None,
            )
        except Exception:
            logger.exception(
                "Could not introspect %s tools — its read tools will keep "
                "asking for approval",
                provider,
            )
            derived[provider] = frozenset()
            complete = False
            continue
        derived[provider] = frozenset(
            d.name
            for d in definitions
            if d.name
            and d.name not in HAND_GATED
            and infer_tool_type(d.name) == "read"
        )
    return derived, complete


def read_tools_for(providers: frozenset[str]) -> frozenset[str]:
    """Read-classified tool names contributed by ``providers``.

    ``providers`` is the set a registration speaks for, so a platform nobody
    has connected exempts nothing — the entry would be inert anyway (its tools
    do not exist), but listing 62 OpenGRC names on a deployment that has never
    heard of OpenGRC would make the one screen answering "what runs without
    asking?" impossible to read.
    """
    by_provider = platform_read_tools()
    return frozenset(
        name
        for provider in providers & PLATFORM_PROVIDERS
        for name in by_provider.get(provider, frozenset())
    )


def provider_of(tool_key: str) -> str | None:
    """Which platform contributes ``tool_key``, or ``None``.

    Used to attribute an entry on the whitelist screen and to scope the bulk
    "re-gate this platform's reads" action. Answered from the derived sets
    rather than by parsing the name, because ``full_text_search_openaev`` and
    ``search_opencti`` put the platform in different places — and one of them
    does not repeat it at all.
    """
    for provider, names in platform_read_tools().items():
        if tool_key in names:
            return provider
    return None
