# SPDX-License-Identifier: Apache-2.0

"""CTEM source readers record signals, never cases (#1529).

The OpenCTI and OpenAEV readers of ``ctem_cases_source`` find open issues
(a vulnerability on inventory software, a CVE finding on an asset). Each issue
the case rules can key on a fix becomes one CTEM signal, idempotent on its
source, its external id and its fix key: a re-read moves ``last_seen_at``,
never adds a row. The signal goes through the scope gate and a ``case``
outcome opens or joins the case through ``ctem_case_loop.open_from_signal``,
on the remediation key the recompute grouped on (``evidence.case_fix``), so
one fix is still one case. Design: ``dev-docs/ctem-cases.md`` section 4.4.

Mapping (pure, documented in the dev-doc):

- OpenCTI vulnerability related to inventory software -> ``new_vulnerability``
  (lens world), subject the CVE, ``evidence.software`` the matched product.
- OpenAEV finding of type ``cve`` on an asset -> ``proven_exploitable`` (lens
  inside): a test found the CVE on the asset, the proof the gate counts.
"""

from __future__ import annotations

from typing import Any

from app.services import ctem_cases as rules
from app.services.ctem_signals import SignalInput, default_dedupe_key

#: The trigger path each source reader records.
SOURCE_PATHS: dict[str, str] = {"opencti": "new_vulnerability", "openaev": "proven_exploitable"}
#: What a source issue signal is about.
SUBJECT_KIND = "vulnerability"


def signal_of(issue: rules.SourceIssue, fix: rules.Remediation) -> SignalInput | None:
    """The signal of one source issue keyed on *fix*; ``None`` for a source that records none."""
    path = SOURCE_PATHS.get(issue.source)
    if path is None:
        return None
    ref = (issue.cve or issue.name or issue.external_id).strip()
    software = fix.product_label or (issue.product or "").strip() or None
    evidence: dict[str, Any] = {
        "source_issue": issue.member_id,
        "case_fix": {
            "key": fix.key,
            "kind": fix.kind,
            "title": fix.title,
            "fixed_version": fix.fixed_version,
            **({"fixed_versions": dict(fix.fixed_versions)} if fix.fixed_versions else {}),
            "product_label": fix.product_label,
            # The case's running versions count repository packages only for a package CVE (#2223).
            **({"language_package": True} if fix.language_package else {}),
        },
        "software": [software] if software else [],
        "cve": issue.cve,
        "cvss": issue.cvss,
        "epss": issue.epss,
        "kev": issue.kev or None,
        "assets": list(issue.assets),
        "primary_assets": list(issue.primary_assets),
        "installed_version": issue.installed_version,
        "fixed_version": issue.fixed_version,
        "version_note": issue.version_note,
        "retest_status": issue.retest_status,
    }
    evidence = {k: v for k, v in evidence.items() if v not in (None, [], "")}
    return SignalInput(
        trigger_path=path,
        source_provider=issue.source,
        source_external_id=issue.external_id,
        subject_kind=SUBJECT_KIND,
        subject_ref=ref,
        subject_label=issue.name,
        observed_at=issue.first_seen_at,
        evidence=evidence,
        # The same CVE on two products is two fixes, so two signals (one per case key).
        dedupe_key=default_dedupe_key(path, issue.source, f"{issue.external_id}|{fix.key}", SUBJECT_KIND, ref),
    )
