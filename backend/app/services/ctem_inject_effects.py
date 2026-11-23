# SPDX-License-Identifier: Apache-2.0

"""What a proposed CTEM validation inject does to its target (#1458), computed, never worded by a model.

The gate ``ctem.validation.non_readonly`` asks a person to let a validation run
against the organization's assets. Its ask used to be written freely by the
loop orchestrator, so an active exploit (a Nuclei template that unpauses a
workflow, posts a run carrying a command injection and reads the result) was
described as "an HTTP check only". This module reads the inject itself:

- :func:`classify_nuclei_template` reads a Nuclei template (YAML) and answers
  ``read_only``, ``intrusive`` or ``unknown`` with the requests that change
  state, the cleanup they leave owed, and whether the template needs
  credentials the contract does not supply;
- :func:`classify_inject` does the same for any injector: a Nuclei template
  when one is given, the contract's own flag otherwise, else ``unknown``
  (handled as intrusive);
- :func:`validation_gate_ask` builds the gate's ask from that classification:
  an intrusive or unknown inject is never called safe or read-only, the side
  effects and the cleanup are listed, approve is not the recommendation against
  an asset that is not a lab, and a passive option is offered when one exists.

A negative result of an exploit attempt only says the exploit did not land
(the endpoint was not reachable, a precondition was missing, credentials were
absent): it never proves the asset unaffected, so it is inconclusive unless
the check reads the running version and proves the fixed one.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

READ_ONLY = "read_only"
INTRUSIVE = "intrusive"
UNKNOWN = "unknown"
CLASSES: tuple[str, ...] = (READ_ONLY, INTRUSIVE, UNKNOWN)

#: Nuclei tags that say the template acts on the target.
_INTRUSIVE_TAGS = frozenset(
    {"rce", "intrusive", "fuzz", "fuzzing", "dos", "sqli", "lfi-write", "file-upload", "upload"}
)
#: Nuclei tags that say the template logs in with credentials it does not carry.
_CREDENTIAL_TAGS = frozenset({"authenticated", "auth-bypass-required", "default-login"})
#: HTTP methods that change state on a server.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Protocols that run code or drive a browser on behalf of the template.
_CODE_PROTOCOLS = ("code", "javascript", "flow")
#: Protocols whose effect this reader does not judge.
_UNJUDGED_PROTOCOLS = ("headless", "network", "tcp", "websocket", "file")
#: Protocols that only read (DNS answers, TLS handshakes, WHOIS).
_READ_PROTOCOLS = ("dns", "ssl", "whois")
#: Path words that make even a GET a state change (a toggle, an action endpoint).
_STATE_PATH = re.compile(
    r"/(?:paused/(?:true|false)|delete|remove|create|update|upload|exec|execute|run|trigger|reset|install|"
    r"uninstall|shutdown|restart|reboot|enable|disable|dag_runs|setup|register)(?:[/?#.]|$)",
    re.IGNORECASE,
)
#: Body or parameter fragments that inject a command into the target.
_COMMAND_INJECTION = re.compile(r"(?:;|\||&&|\$\(|`)\s*(?:touch|id|whoami|curl|wget|nslookup|ping|sh|bash|cat|echo)\b")
#: An out-of-band callback: the target is made to reach an external interaction server.
_OOB = re.compile(r"\{\{\s*interactsh-url\s*\}\}", re.IGNORECASE)
#: Placeholders for credentials the template expects as variables.
_CREDENTIAL_VARS = re.compile(
    r"\{\{\s*(?:username|password|user|pass|token|api_key|apikey|cookie)\s*\}\}", re.IGNORECASE
)
_REQUEST_LINE = re.compile(r"^\s*([A-Z]+)\s+(\S+)")


@dataclass
class InjectEffects:
    """What one inject does to its target, as the platform read it."""

    classification: str
    side_effects: list[str] = field(default_factory=list)
    cleanup: list[str] = field(default_factory=list)
    needs_credentials: bool = False
    reasons: list[str] = field(default_factory=list)
    source: str = "nuclei_template"
    #: The exact text of an authored payload (a command written for this validation), when one is given.
    command: str = ""

    @property
    def intrusive_or_unknown(self) -> bool:
        return self.classification != READ_ONLY

    @property
    def negative_result(self) -> str:
        """What a no-match of this inject proves."""
        if self.needs_credentials:
            return "meaningless: the template needs credentials the contract does not supply"
        if self.intrusive_or_unknown:
            return "inconclusive: the exploit not landing does not prove the asset unaffected"
        return "conclusive only when the check reads the running version and it is the fixed one"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runs_unauthenticated"] = not self.needs_credentials
        data["negative_result"] = self.negative_result
        return data


def _tags(info: Any) -> set[str]:
    raw = info.get("tags") if isinstance(info, dict) else None
    if isinstance(raw, str):
        return {t.strip().lower() for t in raw.split(",") if t.strip()}
    if isinstance(raw, list):
        return {str(t).strip().lower() for t in raw if str(t).strip()}
    return set()


def _raw_requests(block: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(method, path, body)`` of each raw request of a block."""
    found: list[tuple[str, str, str]] = []
    for raw in block.get("raw") or []:
        text = str(raw).replace("\r\n", "\n")
        head, _, body = text.strip("\n").partition("\n\n")
        line = next((ln for ln in head.splitlines() if ln.strip()), "")
        match = _REQUEST_LINE.match(line)
        if match:
            found.append((match.group(1).upper(), match.group(2), body.strip()))
    return found


def _plain_requests(block: dict[str, Any]) -> list[tuple[str, str, str]]:
    method = str(block.get("method") or "GET").upper()
    body = str(block.get("body") or "")
    paths = block.get("path") or []
    if isinstance(paths, str):
        paths = [paths]
    return [(method, str(path).replace("{{BaseURL}}", "") or "/", body) for path in paths]


def classify_nuclei_template(text: str) -> InjectEffects:
    """Classify a Nuclei template's effect on its target from the template itself."""
    import yaml

    try:
        doc = yaml.safe_load(text or "")
    except yaml.YAMLError:
        doc = None
    if not isinstance(doc, dict):
        return InjectEffects(UNKNOWN, reasons=["the template could not be read: its effect is unknown"])
    tags = _tags(doc.get("info"))
    effects = InjectEffects(READ_ONLY)
    effects.needs_credentials = bool(tags & _CREDENTIAL_TAGS) or bool(_CREDENTIAL_VARS.search(text))
    if effects.needs_credentials:
        effects.reasons.append("the template logs in with credentials it expects as variables")
    intrusive_tags = sorted(tags & _INTRUSIVE_TAGS)
    if intrusive_tags:
        effects.classification = INTRUSIVE
        effects.reasons.append("tagged " + ", ".join(intrusive_tags))
    for protocol in _CODE_PROTOCOLS:
        if doc.get(protocol):
            effects.classification = INTRUSIVE
            effects.side_effects.append(f"runs a {protocol} step against the target")
    for protocol in _UNJUDGED_PROTOCOLS:
        if doc.get(protocol) and effects.classification == READ_ONLY:
            effects.classification = UNKNOWN
            effects.reasons.append(f"a {protocol} step: its effect is not judged")
    blocks = [b for key in ("http", "requests") for b in (doc.get(key) or []) if isinstance(b, dict)]
    known = any(doc.get(p) for p in (*_CODE_PROTOCOLS, *_UNJUDGED_PROTOCOLS, *_READ_PROTOCOLS))
    if not blocks and not known:
        return InjectEffects(UNKNOWN, reasons=["no request the reader knows: its effect is unknown"])
    for block in blocks:
        if block.get("payloads") or block.get("fuzzing") or block.get("attack"):
            effects.classification = INTRUSIVE
            effects.side_effects.append("sends a list of attack payloads to the target")
        for method, path, body in (*_raw_requests(block), *_plain_requests(block)):
            where = f"{method} {path}"
            if method in _WRITE_METHODS:
                effects.classification = INTRUSIVE
                effects.side_effects.append(f"{where}: a request that changes state on the target")
            elif _STATE_PATH.search(path):
                effects.classification = INTRUSIVE
                effects.side_effects.append(f"{where}: a request whose path changes state on the target")
            elif body:
                effects.classification = INTRUSIVE if effects.classification == READ_ONLY else effects.classification
                effects.side_effects.append(f"{where}: a request with a body")
            if _COMMAND_INJECTION.search(body) or _COMMAND_INJECTION.search(path):
                effects.classification = INTRUSIVE
                effects.side_effects.append(f"{where}: command injection executed on the target")
            if _OOB.search(body) or _OOB.search(path):
                effects.classification = INTRUSIVE
                effects.side_effects.append(f"{where}: makes the target call an external interaction server")
    if effects.classification == INTRUSIVE:
        effects.cleanup.append(
            "check the target for what the requests above changed (toggled settings, created jobs or runs, "
            "files written by an injected command) and revert it"
        )
    return effects


_VERSION_WORDS = re.compile(r"version|banner|fingerprint", re.IGNORECASE)


def _template_meta(text: str) -> tuple[str, bool]:
    """The template's id (lower case) and whether it reads a version (an extractor naming one)."""
    import yaml

    try:
        doc = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return "", False
    if not isinstance(doc, dict):
        return "", False
    reads = False
    for key in ("http", "requests", "network", "tcp"):
        for block in doc.get(key) or []:
            if isinstance(block, dict) and block.get("extractors"):
                reads = reads or bool(_VERSION_WORDS.search(str(block.get("extractors"))))
    return str(doc.get("id") or "").strip().lower(), reads


@dataclass
class Coverage:
    """Which requested CVE or template ids a selected template set actually tests."""

    covered: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    reads_version: bool = False

    @property
    def state(self) -> str:
        if not self.covered:
            return "none"
        return "partial" if self.uncovered else "full"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state}


def template_coverage(templates: list[str], requested_ids: list[str]) -> Coverage:
    """Resolve each requested id against the templates given: an id with no template is not tested."""
    metas = [_template_meta(t) for t in templates if t and t.strip()]
    ids = {m[0] for m in metas if m[0]}
    cover = Coverage(reads_version=any(m[1] for m in metas))
    for wanted in requested_ids:
        name = str(wanted).strip()
        if name:
            (cover.covered if name.lower() in ids else cover.uncovered).append(name)
    if not requested_ids:
        cover.covered = sorted(ids)
    return cover


def classify_templates(templates: list[str]) -> InjectEffects:
    """The effects of several templates run together: the worst class, every side effect."""
    parts = [classify_nuclei_template(t) for t in templates if t and t.strip()]
    if not parts:
        return InjectEffects(UNKNOWN, reasons=["no template given: its effect is unknown"])
    rank = {READ_ONLY: 0, UNKNOWN: 1, INTRUSIVE: 2}
    merged = InjectEffects(max((p.classification for p in parts), key=rank.__getitem__))
    for part in parts:
        merged.side_effects += part.side_effects
        merged.cleanup += [c for c in part.cleanup if c not in merged.cleanup]
        merged.reasons += part.reasons
        merged.needs_credentials = merged.needs_credentials or part.needs_credentials
    return merged


#: How long an authored command may be when it is embedded in an ask.
COMMAND_MAX = 2000


def classify_inject(
    *,
    injector: str = "",
    template: str | None = None,
    contract_read_only: bool | None = None,
    authored_payload: bool = False,
    command: str = "",
) -> InjectEffects:
    """Classify any proposed inject: its Nuclei template, else its contract's own flag, else unknown.

    *authored_payload*: the payload is written for this validation (a new command payload) or does not
    exist yet. Nothing the platform can read vouches for it, so it is ``unknown`` (handled as intrusive)
    whatever the contract says, and *command*, its exact text, travels with it into the ask.
    """
    text = (command or "").strip()[:COMMAND_MAX]
    if authored_payload or text:
        label = (injector or "this payload").strip()
        return InjectEffects(
            UNKNOWN,
            side_effects=["runs a command authored for this validation on each target"],
            cleanup=["revert what the command changed on each target"],
            reasons=[
                f"{label}: an authored or not-yet-existing payload, no template or contract vouches for it, "
                "handled as intrusive"
            ],
            source="authored_payload",
            command=text,
        )
    if template and template.strip():
        return classify_nuclei_template(template)
    if contract_read_only is True:
        return InjectEffects(READ_ONLY, reasons=["the injector contract says it only reads"], source="contract")
    if contract_read_only is False:
        return InjectEffects(
            INTRUSIVE,
            side_effects=["the injector contract says it changes the target"],
            cleanup=["revert what the inject changed on the target"],
            reasons=["the injector contract says it is not read-only"],
            source="contract",
        )
    label = (injector or "this injector").strip()
    return InjectEffects(
        UNKNOWN,
        reasons=[f"{label}: no template and no read-only flag on the contract, handled as intrusive"],
        source="none",
    )


#: The remediation kinds a version inventory resolves (a patch or an upgrade): a test never does.
VERSION_REMEDIATIONS: tuple[str, ...] = ("patch", "upgrade")
#: What a validation reference proves: the fix in place (a version inventory), or a test's behaviour.
REMEDIATION_EVIDENCE = "remediation"
VALIDATION_EVIDENCE = "validation"
EVIDENCE_TYPES: tuple[str, ...] = (REMEDIATION_EVIDENCE, VALIDATION_EVIDENCE)
_REMEDIATION_EVIDENCE_TEXT = "a version inventory showing every in-scope endpoint at or above the fixed build"
#: Words a non-read-only gate never carries in its title or text.
_SAFE_WORDS = re.compile(r"\b(?:read[- ]only|safe(?:ly)?|only reads?|harmless|non[- ]intrusive|passive)\b", re.I)


def gate_title(classification: str, subject: str) -> str:
    """The title of the gate ``ctem.validation.non_readonly``, from the inject class, never from a model."""
    label = {
        READ_ONLY: "Read-only validation",
        INTRUSIVE: "Intrusive validation",
    }.get(classification, "Validation with unknown effects (handled as intrusive)")
    return f"{label}: {subject}"[:200]


def safe_mode_text(classification: str) -> str:
    """The safe-mode line of the gate: yes only for an inject the platform read as read-only."""
    if classification == READ_ONLY:
        return "Safe mode: yes, every request only reads."
    if classification == INTRUSIVE:
        return "Safe mode: no, it acts on the target."
    return "Safe mode: no, its effect could not be read; handled as intrusive."


def wording_problems(classification: str, text: str) -> list[str]:
    """What in a drafted gate title or text contradicts the inject class (a non-read-only gate called safe)."""
    if classification == READ_ONLY or not (text or "").strip():
        return []
    allowed = safe_mode_text(classification)
    found = sorted({m.group(0).lower() for m in _SAFE_WORDS.finditer(text.replace(allowed, ""))})
    return [f"a {classification} inject is described as {word!r}" for word in found]


def target_line(target_count: int | None, inventory_count: int | None = None, inventory_label: str = "") -> str:
    """The targets an inject runs on and their delta against the inventory the context cites."""
    if not isinstance(target_count, int) or target_count < 0:
        return "Targets: not counted; count the targets before any launch."
    line = f"Targets: {target_count}."
    if isinstance(inventory_count, int) and inventory_count >= 0:
        label = f" {inventory_label.strip()}" if inventory_label.strip() else ""
        delta = target_count - inventory_count
        if delta == 0:
            line = f"Targets: {target_count}, as many as the {inventory_count}{label} the inventory cites."
        else:
            way = "more" if delta > 0 else "fewer"
            line = (
                f"Targets: {target_count}, {abs(delta)} {way} than the {inventory_count}{label} the inventory "
                f"cites; confirm the difference before any launch."
            )
    return line


def validation_gate_ask(
    effects: InjectEffects,
    *,
    subject: str,
    target_is_lab: bool = False,
    passive_option: str = "",
    coverage: Coverage | None = None,
    target_count: int | None = None,
    inventory_count: int | None = None,
    inventory_label: str = "",
    remediation_kind: str = "",
    passive_reads_version: bool = False,
) -> dict[str, Any]:
    """The ask of the gate ``ctem.validation.non_readonly`` for one inject, built from its classification.

    *subject*: what the validation tests (a CVE id, a threat), *passive_option*: a check that only reads
    (a version or banner read, a read-only template), offered when given. The title and the safe-mode line
    follow the class (#1500); an authored payload carries its exact command, or asks for a payload review
    before the launch decision when its text is not given; the target count and its delta against the
    cited inventory are listed; on a patch or upgrade case each option names the evidence it produces.
    """
    authored = effects.source == "authored_payload"
    lines: list[str] = []
    if effects.classification == READ_ONLY:
        lines.append("What it does on the target: it sends requests that only read (no request changes state).")
    else:
        state = (
            "acts on the target" if effects.classification == INTRUSIVE else "has an effect the platform could not read"
        )
        lines.append(f"What it does on the target: it {state}.")
        lines.extend(f"- {effect}" for effect in effects.side_effects)
        if effects.cleanup:
            lines.append("Cleanup owed after the run: " + "; ".join(effects.cleanup) + ".")
    if authored and effects.command:
        lines.append("Command, exact text:\n" + effects.command)
    lines.append(target_line(target_count, inventory_count, inventory_label))
    lines.append(
        "Runs without credentials: no, a no-match proves nothing."
        if effects.needs_credentials
        else "Runs without credentials: yes."
    )
    if coverage is not None:
        lines.append("Tested: " + (", ".join(coverage.covered) or "nothing (no template matches)") + ".")
        if coverage.uncovered:
            lines.append("Not tested (no template): " + ", ".join(coverage.uncovered) + ".")
        if not coverage.reads_version:
            lines.append("It does not read the running version.")
    lines.append("A no-match is " + effects.negative_result + "; the case stays open with its remediation.")
    version_case = (remediation_kind or "").strip().lower() in VERSION_REMEDIATIONS
    if version_case:
        lines.append(
            "This case is resolved only by remediation evidence: " + _REMEDIATION_EVIDENCE_TEXT + "; a detection "
            "or behaviour test result is recorded as validation evidence and never resolves it."
        )
    if coverage is not None and coverage.covered and coverage.uncovered:
        subject = ", ".join(coverage.covered)
    if authored and not effects.command:
        # Nothing to judge yet: the payload is reviewed first, the launch is a later decision.
        options: list[dict[str, str]] = [
            {
                "key": "A",
                "label": "Review the payload first: show its exact text, then decide the launch",
                "effect": "Nothing runs; the launch is a separate decision once the payload text is read.",
            },
            {
                "key": "B",
                "label": "Do not run it",
                "effect": "No validation; the case stays open with its remediation.",
            },
        ]
        question = f"Review the authored {subject} payload before any launch decision?"
        recommendation = "A"
    else:
        options = [
            {"key": "A", "label": f"Run it against the asset{' (a lab)' if target_is_lab else ''}", "effect": lines[0]},
        ]
        if passive_option.strip():
            options.append(
                {
                    "key": "B",
                    "label": passive_option.strip(),
                    "effect": "A passive check; nothing is sent beyond reads.",
                }
            )
        options.append(
            {
                "key": "C" if passive_option.strip() else "B",
                "label": "Do not run it",
                "effect": "No validation; the case stays open with its remediation.",
            }
        )
        question = f"Run the {subject} validation inject against the asset?"
        # Against an asset that is not a lab, an intrusive or unknown inject is never the recommendation:
        # B is the passive check when one is offered, else not running it.
        recommendation = "A" if effects.classification == READ_ONLY or target_is_lab else "B"
    if version_case:
        for option in options:
            if option["label"] == "Do not run it" or option["label"].startswith("Review the payload"):
                kind = "none"
            elif option["key"] == "B" and passive_reads_version:
                kind = REMEDIATION_EVIDENCE
            else:
                kind = VALIDATION_EVIDENCE
            option["evidence"] = kind
            option["effect"] += {
                "none": " Evidence: none.",
                REMEDIATION_EVIDENCE: " Evidence: remediation (a version inventory); it can resolve the case.",
                VALIDATION_EVIDENCE: " Evidence: validation only; it never resolves the case.",
            }[kind]
    return {
        "title": gate_title(effects.classification, subject),
        "safe_mode": effects.classification == READ_ONLY,
        "safe_mode_text": safe_mode_text(effects.classification),
        "question": question,
        "context": "\n".join(lines),
        "options": options,
        "recommendation": recommendation,
        "classification": effects.classification,
        "payload_review_first": authored and not effects.command,
    }


def mobilize_done_criterion(
    effects: InjectEffects | None,
    *,
    remediation_kind: str = "",
    target_count: int | None = None,
    version_check: str = "",
) -> dict[str, Any]:
    """The done criterion of a remediation ticket and the lines the gate ``ctem.mobilize`` carries (#1500).

    The re-test that closes a ticket follows the inject class: a read-only re-test may be the criterion;
    an intrusive or unknown one never silently: a non-intrusive check is named (*version_check*, a version
    read) or the re-test is stated intrusive with its side effects and targets. A negative exploit re-test is
    inconclusive and never closes a ticket alone. The hold option depends on no other pending gate.
    """
    klass = effects.classification if effects is not None else UNKNOWN
    version_case = (remediation_kind or "").strip().lower() in VERSION_REMEDIATIONS
    check = (version_check or "").strip()
    lines: list[str] = []
    if version_case:
        criterion = "Done when " + _REMEDIATION_EVIDENCE_TEXT + (f" ({check})" if check else "") + "."
        evidence = REMEDIATION_EVIDENCE
    elif klass == READ_ONLY:
        criterion = "Done when the read-only re-test passes and reads the fixed state."
        evidence = VALIDATION_EVIDENCE
    elif check:
        criterion = f"Done when this non-intrusive check proves the fix: {check}."
        evidence = VALIDATION_EVIDENCE
    else:
        effects_text = (
            "; ".join(effects.side_effects)
            if effects is not None and effects.side_effects
            else "its effect could not be read"
        )
        targets = (
            f"{target_count} target(s)"
            if isinstance(target_count, int) and target_count >= 0
            else "targets not counted yet"
        )
        criterion = (
            "Done when the re-test proves the fix. The re-test is intrusive: "
            + effects_text
            + f"; it runs on {targets} and needs its own approval."
        )
        evidence = VALIDATION_EVIDENCE
    lines.append("Done criterion: " + criterion)
    if klass != READ_ONLY:
        lines.append("A negative exploit re-test is inconclusive: it never closes the ticket alone.")
    hold = {
        "key": "hold",
        "label": "Hold: create no ticket now",
        "effect": "No ticket, no notice; the case stays open with its remediation and is asked again at its next step.",
    }
    return {
        "done_criterion": criterion,
        "evidence": evidence,
        "retest_class": klass,
        "negative_retest": "inconclusive" if klass != READ_ONLY else "conclusive only with the fixed version read",
        "lines": lines,
        "hold_option": hold,
    }


def checked_safe_mode(safe_mode: bool | None, inject_class: str | None) -> tuple[bool | None, str | None]:
    """``safe_mode`` as a validation may record it: true only for an inject the platform read as read-only."""
    if safe_mode is True and inject_class != READ_ONLY:
        return False, (
            "safe_mode recorded false: only an inject assess_ctem_validation_inject classified read_only "
            f"is safe mode (this one: {inject_class or 'not assessed'})"
        )
    return safe_mode, None


__all__ = [
    "CLASSES",
    "EVIDENCE_TYPES",
    "INTRUSIVE",
    "READ_ONLY",
    "REMEDIATION_EVIDENCE",
    "UNKNOWN",
    "VALIDATION_EVIDENCE",
    "VERSION_REMEDIATIONS",
    "Coverage",
    "InjectEffects",
    "checked_safe_mode",
    "classify_inject",
    "classify_nuclei_template",
    "classify_templates",
    "gate_title",
    "mobilize_done_criterion",
    "safe_mode_text",
    "target_line",
    "template_coverage",
    "validation_gate_ask",
    "wording_problems",
]
