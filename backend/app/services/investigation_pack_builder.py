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

"""Deep Investigation packs users build (the pack builder).

A built pack is a method stored in the database (`InvestigationPack`): ordered
steps, each served by sources of the kinds the platform already queries, and
choices offered at launch. This module owns two things:

- **the definition** (`PackDefinition`): validated where the request body is
  parsed, so a bad definition is a 422 and never reaches a run; the same model
  re-validates a stored one before it is used;
- **the conversion** (`build_source_pack`): a definition becomes a
  `SourcePack` FOR ONE CALLER. Every integration-backed source is resolved
  against what that caller may use (their VirusTotal, the registered OpenCTI
  they can read, the corpora and MCP servers they can see), never against the
  author's: a shared pack never lends its author's access. A source the caller
  cannot use is left out and said in the pack's description; its step stays
  in the plan, drawn as one nothing serves.

The steps become a `GoalPlan`: step *i* requires the effect of the step before
it, the cited report comes last, and a step none of whose sources applies to a
subject type is already satisfied for that type, so the plan skips it the way
the built-in plan skips qualifying a seed for an IP. A step's sources serve
the action ``step:<key>``, a namespace no built-in action uses.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.investigation_packs import (
    ACTION_WRITE_REPORT,
    DEFAULT_GOAL_OBJECTIVE,
    FINGERPRINT_SETS,
    GOAL_ACTION_ICONS,
    HOSTNAME_KEYWORD_SETS,
    NEWS_OUTLET_KEYWORDS,
    ONE_SUBJECT_TYPES,
    PASSIVE_DNS_MAX_PAGES,
    PIVOT_DEPTH_OPTION,
    SUBJECT_TYPES,
    WEB_GENERAL,
    WEB_RESEARCH,
    WORDPRESS_MEDIA_NETWORK,
    GoalAction,
    GoalPlan,
    KnowledgeBaseSelector,
    PackChoice,
    PackOption,
    PackOwner,
    SourceDefinition,
    SourcePack,
    _check_query_template,
    opencti_source,
)

#: A built pack's slug in runs and in the picker; never a built-in slug.
BUILT_PACK_PREFIX = "pack:"
#: The action a built step's sources serve: ``step:<key>``.
STEP_ACTION_PREFIX = "step:"

MAX_STEPS = 12
MAX_STEP_SOURCES = 12
MAX_EXPOSED_SETTINGS = 6
MAX_PACKS_PER_OWNER = 200
MAX_ITERATIONS_CEILING = 50

_STEP_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

#: The subject types each kind of source can answer. A step's sources answer
#: the step's types that their kind supports.
KIND_SUBJECT_TYPES: dict[str, tuple[str, ...]] = {
    "opencti": ("domain", "url", "ip", "campaign", "threat_actor", "domain_list", "unknown"),
    "passive_dns": ("domain", "url", "ip", "domain_list"),
    "site_fingerprint": ("domain", "url"),
    "web_search": ONE_SUBJECT_TYPES,
    "knowledge_base": ONE_SUBJECT_TYPES,
    "mcp_tool": ONE_SUBJECT_TYPES,
    # A public reference source answers what its own definition says.
    "reference": ONE_SUBJECT_TYPES,
    # A URL its author typed: fetched pinned (`pinned_fetch`).
    "http": ONE_SUBJECT_TYPES,
}
BUILDER_SOURCE_KINDS = tuple(KIND_SUBJECT_TYPES)

#: How a source of each kind is named in a run when its spec names none.
_KIND_NAMES = {
    "opencti": "OpenCTI knowledge",
    "passive_dns": "VirusTotal passive DNS",
    "site_fingerprint": "Site fingerprint",
    "web_search": "Web research",
    "knowledge_base": "Knowledge base",
    "mcp_tool": "MCP tool",
    "reference": "Reference source",
    "http": "HTTP request",
}

#: The public reference sources a step may use, by name (`web-general`'s).
REFERENCE_SOURCES: dict[str, SourceDefinition] = {s.name: s for s in WEB_GENERAL.sources}

#: Settings a source exposes at launch, by kind, and their choices.
EXPOSABLE_SETTINGS: dict[str, tuple[str, ...]] = {"passive_dns": ("pivot_scope", "max_pages", "hostname_keywords")}
_SETTING_LABELS = {
    "pivot_scope": PIVOT_DEPTH_OPTION.label,
    "max_pages": "VirusTotal pages",
    "hostname_keywords": "Host-name filter",
}
_SETTING_CHOICES: dict[str, tuple[PackChoice, ...]] = {
    "pivot_scope": PIVOT_DEPTH_OPTION.choices,
    "max_pages": tuple(PackChoice(str(n), f"{n} page{'s' if n > 1 else ''}", "") for n in (1, 5, 10, 25)),
    "hostname_keywords": (
        PackChoice(NEWS_OUTLET_KEYWORDS.slug, "Newsroom-named sites only", ""),
        PackChoice("none", "Every site", ""),
    ),
}
_STEP_CHOICES = (PackChoice("on", "On", ""), PackChoice("off", "Off", ""))


def plain_text(value: str | None, *, multiline: bool = False) -> str | None:
    """Text a person wrote for others to read: no control or invisible
    formatting character (a bidi override, a zero-width space), and on one
    line unless ``multiline``. Refused rather than cleaned: what is stored is
    what its author typed."""
    if value is None:
        return None
    for char in value:
        if multiline and char == "\n":
            continue
        if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp"):
            raise ValueError(
                "use plain text: no invisible or control characters"
                if multiline
                else "use plain text: no line breaks or invisible characters"
            )
    return value


def check_url_template(template: str | None) -> str:
    """A free HTTP source's URL: http or https, the subject placed by exactly
    one bare ``{value}``, no credentials. Where it leads is checked when it is
    fetched (`pinned_fetch`), never here: a name public today can resolve
    inward tomorrow. Parsed by `httpx.URL`, the parser the fetch connects with
    (`pinned_fetch.fetched_url`), so the host and the credentials judged here
    are the ones the fetch sees."""
    from string import Formatter

    import httpx

    if not template:
        raise ValueError("an HTTP source has a URL template")
    if not template.lower().startswith(("http://", "https://")):
        raise ValueError("an HTTP source's URL starts with http:// or https://")
    try:
        fields = [(f, spec, conv) for _t, f, spec, conv in Formatter().parse(template) if f is not None]
    except ValueError as exc:
        raise ValueError(f"the URL template is malformed: {exc}") from exc
    if len(fields) != 1 or fields[0] != ("value", "", None):
        raise ValueError("the URL template places the subject through exactly one {value}, and nothing else in braces")
    try:
        parsed = httpx.URL(template.replace("{value}", "subject"))
    except httpx.InvalidURL as exc:
        raise ValueError(f"the URL template is malformed: {exc}") from exc
    if not parsed.raw_host:
        raise ValueError("the URL template names a host")
    if parsed.userinfo:
        raise ValueError("the URL template carries no credentials: an integration holds them")
    return template


class PackDefinitionError(ValueError):
    """A stored definition this build cannot run (validated again on read)."""


# ── The definition ───────────────────────────────────────────────────────────


class SourceSpec(BaseModel):
    """One source of a step. Fields not of its kind are refused."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "opencti", "passive_dns", "site_fingerprint", "web_search", "knowledge_base", "mcp_tool", "reference", "http"
    ]
    name: str | None = Field(default=None, max_length=80)
    #: Narrower than the step's, e.g. a fingerprint in a step on domains and IPs.
    subject_types: list[str] | None = None
    # reference
    reference: str | None = None
    # passive_dns
    hostname_keywords: str | None = None
    max_pages: int | None = Field(default=None, ge=1, le=PASSIVE_DNS_MAX_PAGES)
    pivot_scope: Literal["shared", "all"] = "shared"
    pivot_step: str | None = None
    # site_fingerprint
    fingerprint_set: str | None = None
    # web_search
    search_prompt: str | None = Field(default=None, max_length=2000)
    # knowledge_base
    kb_slug: str | None = Field(default=None, max_length=120)
    kb_tags: list[str] | None = Field(default=None, max_length=8)
    knowledge_base_ids: list[str] | None = Field(default=None, max_length=12)
    query_template: str | None = Field(default=None, max_length=500)
    min_similarity: float | None = None
    # mcp_tool
    server_id: str | None = Field(default=None, max_length=64)
    # http
    url_template: str | None = Field(default=None, max_length=500)
    value_transform: Literal["host", "slug"] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return plain_text(value)

    @field_validator("search_prompt", "query_template")
    @classmethod
    def _prompt(cls, value: str | None) -> str | None:
        return plain_text(value, multiline=True)

    @model_validator(mode="after")
    def _fields_of_its_kind(self) -> SourceSpec:
        own = {
            "reference": {"reference"},
            "passive_dns": {"hostname_keywords", "max_pages", "pivot_scope", "pivot_step"},
            "site_fingerprint": {"fingerprint_set"},
            "web_search": {"search_prompt"},
            "knowledge_base": {"kb_slug", "kb_tags", "knowledge_base_ids", "query_template", "min_similarity"},
            "mcp_tool": {"server_id"},
            "http": {"url_template", "value_transform"},
            "opencti": set(),
        }[self.kind]
        foreign = sorted(
            f
            for f in self.model_fields_set - {"kind", "name", "subject_types"} - own
            if getattr(self, f) not in (None, "shared")
        )
        if foreign:
            raise ValueError(f"a {self.kind} source takes no {', '.join(foreign)}")
        if self.kind == "reference" and self.reference not in REFERENCE_SOURCES:
            raise ValueError(f"unknown reference source {self.reference!r}; known: {sorted(REFERENCE_SOURCES)}")
        if self.hostname_keywords is not None and self.hostname_keywords not in HOSTNAME_KEYWORD_SETS:
            raise ValueError(f"unknown host-name keywords {self.hostname_keywords!r}")
        if self.kind == "site_fingerprint" and self.fingerprint_set not in (None, *FINGERPRINT_SETS):
            raise ValueError(f"unknown fingerprint set {self.fingerprint_set!r}")
        if self.kind == "web_search" and self.search_prompt is not None:
            # The rule the run formats it by, so a prompt saved is one it runs.
            try:
                _check_query_template("web search prompt", self.search_prompt, allow_empty=False)
            except ValueError:
                raise ValueError(
                    "a web search prompt places the subject through {value} and nothing else in braces "
                    "(write {{ and }} for a brace)"
                ) from None
            if self.search_prompt.count("{value}") != 1:
                raise ValueError("a web search prompt places the subject through exactly one {value}")
        if self.kind == "mcp_tool" and not self.server_id:
            raise ValueError("an MCP tool source names its server")
        if self.kind == "http":
            check_url_template(self.url_template)
        if self.kind == "knowledge_base":
            # The same constructor a declared corpus goes through: one rule.
            self.selector(("domain",), None)
        return self

    def selector(self, subject_types: tuple[str, ...], action: str | None) -> KnowledgeBaseSelector:
        return KnowledgeBaseSelector(
            slug=self.kb_slug or None,
            tags=tuple(self.kb_tags or ()),
            ids=tuple(self.knowledge_base_ids or ()),
            subject_types=subject_types,
            query_template=self.query_template or "",
            min_similarity=self.min_similarity,
            action=action,
        )


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    icon: str = "target"
    optional: bool = False
    default_on: bool = True
    subject_types: list[str] | None = None
    sources: list[SourceSpec] = Field(default_factory=list, max_length=MAX_STEP_SOURCES)

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return plain_text(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return plain_text(value, multiline=True)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _STEP_KEY.match(value):
            raise ValueError("a step key is 1-40 lowercase letters, digits, '-' or '_', starting with a letter or digit")
        return value

    @field_validator("icon")
    @classmethod
    def _icon(cls, value: str) -> str:
        if value not in GOAL_ACTION_ICONS:
            raise ValueError(f"unknown icon {value!r}; known: {sorted(GOAL_ACTION_ICONS)}")
        return value

    @property
    def action(self) -> str:
        return f"{STEP_ACTION_PREFIX}{self.key}"


class ExposedSetting(BaseModel):
    """A source setting offered at launch, with its default."""

    model_config = ConfigDict(extra="forbid")

    step: str
    source: int = Field(ge=0)
    setting: Literal["pivot_scope", "max_pages", "hostname_keywords"]
    label: str | None = Field(default=None, max_length=120)
    default: str

    @field_validator("label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return plain_text(value)

    @property
    def key(self) -> str:
        return f"{self.step}.{self.source}.{self.setting}"


class PackDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[StepSpec] = Field(min_length=1, max_length=MAX_STEPS)
    exposed: list[ExposedSetting] = Field(default_factory=list, max_length=MAX_EXPOSED_SETTINGS)

    @model_validator(mode="after")
    def _coherent(self) -> PackDefinition:
        keys = [s.key for s in self.steps]
        if len(set(keys)) != len(keys):
            raise ValueError("every step has its own key")
        position = {key: i for i, key in enumerate(keys)}
        fed: set[str] = set()
        for i, step in enumerate(self.steps):
            for source in step.sources:
                if source.pivot_step is None:
                    continue
                if source.pivot_step not in position or position[source.pivot_step] < i:
                    raise ValueError(f"step {step.key!r} pivots into {source.pivot_step!r}, which is not it or a later step")
                fed.add(source.pivot_step)
        for step in self.steps:
            if not step.sources and step.key not in fed:
                raise ValueError(f"step {step.key!r} has no source and no step pivots into it")
        seen: set[str] = set()
        by_key = {s.key: s for s in self.steps}
        for exposed in self.exposed:
            if exposed.key in seen:
                raise ValueError(f"setting {exposed.key!r} is exposed twice")
            seen.add(exposed.key)
            step = by_key.get(exposed.step)
            if step is None or exposed.source >= len(step.sources):
                raise ValueError(f"setting {exposed.key!r} names no source")
            source = step.sources[exposed.source]
            if exposed.setting not in EXPOSABLE_SETTINGS.get(source.kind, ()):
                raise ValueError(f"a {source.kind} source exposes no {exposed.setting}")
            if exposed.default not in {c.value for c in _SETTING_CHOICES[exposed.setting]}:
                raise ValueError(f"setting {exposed.key!r} defaults to a value it does not take")
        return self


class PackFields(BaseModel):
    """What a built pack carries besides its sharing: validated together, since
    a step's subject types are a subset of the pack's."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    subject_types: list[str] = Field(min_length=1)
    recommended: bool = False
    max_iterations: int = Field(default=10, ge=1, le=MAX_ITERATIONS_CEILING)
    definition: PackDefinition

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a pack has a name")
        return plain_text(value.strip())

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return plain_text(value, multiline=True)

    @field_validator("subject_types")
    @classmethod
    def _types(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(SUBJECT_TYPES))
        if unknown:
            raise ValueError(f"unknown subject types {unknown}")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _steps_within_the_pack(self) -> PackFields:
        for step in self.definition.steps:
            outside = sorted(set(step.subject_types or ()) - set(self.subject_types))
            if outside:
                raise ValueError(f"step {step.key!r} answers {outside}, which the pack does not")
            for i, source in enumerate(step.sources):
                if not _source_types(self.subject_types, step, source):
                    raise ValueError(f"source {i} of step {step.key!r} answers none of the step's subject types")
        by_key = {s.key: s for s in self.definition.steps}
        for exposed in self.definition.exposed:
            step = by_key[exposed.step]
            types = _source_types(self.subject_types, step, step.sources[exposed.source])
            if exposed.setting == "pivot_scope" and "domain_list" not in types:
                raise ValueError(f"setting {exposed.key!r}: only a source answering a list of domains has a pivot depth")
        return self


def parse_definition(data: Any) -> PackDefinition:
    """A stored definition, validated again: what is read is what was allowed."""
    try:
        return PackDefinition.model_validate(data)
    except ValueError as exc:
        raise PackDefinitionError(str(exc)) from exc


# ── The conversion, per caller ───────────────────────────────────────────────


@dataclass(frozen=True)
class CallerResources:
    """What a caller may use, read once by the resolver for every pack."""

    virustotal: tuple[str, str] | None = None
    opencti: tuple[str, str] | None = None
    #: ``(server id, server name, tool, argument)`` of the designated MCP tools.
    mcp_tools: Sequence[tuple[str, str, str, str | None]] = ()


@dataclass(frozen=True)
class _StepMeta:
    action: str
    label: str
    description: str
    icon: str
    fed_by: str | None
    #: Subject types none of the step's sources (nor its feeder's) answers.
    skipped_for: frozenset[str]


def built_pack_slug(pack_id: Any) -> str:
    return f"{BUILT_PACK_PREFIX}{pack_id}"


def _source_types(pack_types: Sequence[str], step: StepSpec, source: SourceSpec) -> tuple[str, ...]:
    step_types = step.subject_types or list(pack_types)
    kind_types = KIND_SUBJECT_TYPES[source.kind]
    if source.kind == "reference":
        kind_types = REFERENCE_SOURCES[str(source.reference)].subject_types
    own = source.subject_types
    return tuple(t for t in step_types if t in kind_types and (own is None or t in own))


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name} ({n})", n + 1
    taken.add(candidate)
    return candidate


def steps_plan(metas: Sequence[_StepMeta], subject_types: Sequence[str]) -> GoalPlan:
    """The plan of the steps switched on: each requires the one before it,
    the report comes last, a step a subject type skips is given for it."""
    predicates = [("subject_defined", "Subject defined")]
    actions: list[GoalAction] = []
    previous = "subject_defined"
    for meta in metas:
        effect = f"{meta.action}:done"
        predicates.append((effect, meta.label))
        actions.append(
            GoalAction(
                slug=meta.action,
                label=meta.label,
                description=meta.description,
                cost=1,
                preconditions=(previous,),
                effects=(effect,),
                icon=meta.icon,
                fed_by=meta.fed_by if meta.fed_by in {m.action for m in metas} else None,
            )
        )
        previous = effect
    predicates.append(("report_written", "Cited report written"))
    actions.append(
        GoalAction(
            slug=ACTION_WRITE_REPORT,
            label="Write the cited report",
            description="One report across every step, each sentence traceable to a passage read.",
            cost=1,
            preconditions=(previous,),
            effects=("report_written",),
            icon="file",
            produces_report=True,
        )
    )
    initial = {
        t: ("subject_defined", *(f"{m.action}:done" for m in metas if t in m.skipped_for)) for t in subject_types
    }
    return GoalPlan(
        objective=DEFAULT_GOAL_OBJECTIVE,
        predicates=tuple(predicates),
        goal=tuple(p for p, _label in predicates[1:]),
        actions=tuple(actions),
        initial={**initial, "*": ("subject_defined",)},
    )


def build_source_pack(
    *,
    pack_id: Any,
    name: str,
    description: str,
    subject_types: Sequence[str],
    recommended: bool,
    max_iterations: int,
    version: int,
    definition: PackDefinition,
    resources: CallerResources,
    owner: PackOwner | None = None,
) -> SourcePack:
    """The pack as ``resources``' caller runs it."""
    slug = built_pack_slug(pack_id)
    pack_types = tuple(subject_types)
    sources: list[SourceDefinition] = []
    selectors: list[KnowledgeBaseSelector] = []
    missing: list[str] = []
    names: set[str] = set()
    #: (step key, source index) -> the run's source name, for exposed settings.
    named: dict[tuple[str, int], str] = {}
    mcp = {server_id: (server_name, tool, argument) for server_id, server_name, tool, argument in resources.mcp_tools}
    fed_by: dict[str, str] = {}
    for step in definition.steps:
        for source in step.sources:
            if source.pivot_step is not None:
                fed_by.setdefault(f"{STEP_ACTION_PREFIX}{source.pivot_step}", step.action)

    for step in definition.steps:
        for index, spec in enumerate(step.sources):
            types = _source_types(pack_types, step, spec)
            label = spec.name or _KIND_NAMES[spec.kind]
            built = _build_source(spec, types, step.action, resources, mcp)
            if built is None and spec.kind != "knowledge_base":
                missing.append(f"{step.label}: {label}")
                continue
            if spec.kind == "knowledge_base":
                selectors.append(spec.selector(types, step.action))
                continue
            # An unnamed source whose kind already has a source in the pack is
            # named after its step ("VirusTotal passive DNS (Who else is on
            # that IP)"), never "(2)": the picker shows these names, joined
            # with " · ", so the step is not set off with that separator.
            default = built.name
            if not spec.name and default in names:
                default = f"{default} ({step.label})"
            built = dataclasses.replace(built, name=_unique(spec.name or default, names))
            named[(step.key, index)] = built.name
            sources.append(built)

    metas = []
    for step in definition.steps:
        served = {t for source in step.sources for t in _source_types(pack_types, step, source)}
        feeder = fed_by.get(step.action)
        if feeder is not None:
            feeder_step = next(s for s in definition.steps if s.action == feeder)
            served |= {t for source in feeder_step.sources for t in _source_types(pack_types, feeder_step, source)}
        metas.append(
            _StepMeta(
                action=step.action,
                label=step.label,
                description=step.description,
                icon=step.icon,
                fed_by=feeder if feeder != step.action else None,
                skipped_for=frozenset(t for t in pack_types if t not in served),
            )
        )

    options: list[PackOption] = []
    for step, meta in zip(definition.steps, metas, strict=True):
        if step.optional:
            options.append(
                PackOption(
                    key=f"{step.key}.enabled",
                    label=step.label,
                    description=step.description or "An optional step of this pack.",
                    subject_types=pack_types,
                    choices=_STEP_CHOICES,
                    default="on" if step.default_on else "off",
                    apply=_step_switch(meta, metas, pack_types),
                )
            )
    for exposed in definition.exposed:
        target = named.get((exposed.step, exposed.source))
        if target is None:
            continue  # the source is not available to this caller
        step = next(s for s in definition.steps if s.key == exposed.step)
        options.append(
            PackOption(
                key=exposed.key,
                label=exposed.label or _SETTING_LABELS[exposed.setting],
                description=f"{step.label}: {target}",
                subject_types=tuple(
                    t
                    for t in _source_types(pack_types, step, step.sources[exposed.source])
                    if exposed.setting != "pivot_scope" or t == "domain_list"
                ),
                choices=_SETTING_CHOICES[exposed.setting],
                default=exposed.default,
                apply=_setting_apply(target, exposed.setting),
            )
        )

    text = description.strip()
    if missing:
        text += (" " if text else "") + "Not available to you: " + "; ".join(missing) + "."
    return SourcePack(
        slug=slug,
        label=name,
        description=text,
        subject_types=pack_types,
        sources=tuple(sources),
        max_iterations=max_iterations,
        auto_select=False,
        knowledge_bases=tuple(selectors),
        goal=steps_plan(metas, pack_types),
        options=tuple(options),
        recommended=recommended,
        version=version,
        owner=owner,
    )


def _build_source(
    spec: SourceSpec,
    types: tuple[str, ...],
    action: str,
    resources: CallerResources,
    mcp: dict[str, tuple[str, str, str | None]],
) -> SourceDefinition | None:
    name = spec.name or _KIND_NAMES[spec.kind]
    if spec.kind == "opencti":
        if resources.opencti is None:
            return None
        return opencti_source(name, subject_types=types, action=action)
    if spec.kind == "passive_dns":
        if resources.virustotal is None:
            return None
        return SourceDefinition(
            name=name,
            subject_types=types,
            kind="passive_dns",
            integration_id=resources.virustotal[0],
            value_transform="host",
            hostname_keywords=spec.hostname_keywords,
            max_pages=spec.max_pages,
            pivot_scope=spec.pivot_scope if "domain_list" in types else "shared",
            hop_action=f"{STEP_ACTION_PREFIX}{spec.pivot_step}" if spec.pivot_step else None,
            hop_opencti=resources.opencti is not None,
            action=action,
        )
    if spec.kind == "site_fingerprint":
        return SourceDefinition(
            name=name,
            subject_types=types,
            kind="site_fingerprint",
            fingerprint_set=spec.fingerprint_set or WORDPRESS_MEDIA_NETWORK.slug,
            action=action,
        )
    if spec.kind == "web_search":
        return SourceDefinition(
            name=name,
            subject_types=types,
            kind="web_search",
            search_prompt=spec.search_prompt or WEB_RESEARCH.sources[0].search_prompt,
            action=action,
        )
    if spec.kind == "mcp_tool":
        server = mcp.get(str(spec.server_id))
        if server is None:
            return None
        server_name, tool, argument = server
        return SourceDefinition(
            name=spec.name or f"{server_name}: {tool}",
            subject_types=types,
            kind="mcp_tool",
            mcp_server_id=str(spec.server_id),
            mcp_tool_name=tool,
            mcp_argument=argument,
            action=action,
        )
    if spec.kind == "reference":
        return dataclasses.replace(REFERENCE_SOURCES[str(spec.reference)], subject_types=types, action=action)
    if spec.kind == "http":
        return SourceDefinition(
            name=name,
            subject_types=types,
            kind="http",
            url_template=str(spec.url_template),
            value_transform=spec.value_transform,
            pinned=True,
            action=action,
        )
    return None  # knowledge_base: a selector, resolved with the caller's corpora


def _step_switch(
    meta: _StepMeta, metas: Sequence[_StepMeta], pack_types: tuple[str, ...]
) -> Callable[[SourcePack, str], SourcePack]:
    def _apply(pack: SourcePack, value: str) -> SourcePack:
        if value == "on":
            return pack
        kept = {a.slug for a in (pack.goal.actions if pack.goal else ())} - {meta.action}
        # A source pivoting into the step switched off keeps its own lookup
        # and stops hopping: the step's leads would spend VirusTotal and
        # visit the neighbours all the same.
        return dataclasses.replace(
            pack,
            sources=tuple(
                dataclasses.replace(s, hop_action=None) if s.hop_action == meta.action else s
                for s in pack.sources
                if s.action != meta.action
            ),
            knowledge_bases=tuple(k for k in pack.knowledge_bases if k.action != meta.action),
            goal=steps_plan([m for m in metas if m.action in kept], pack_types),
        )

    return _apply


def _setting_apply(source_name: str, setting: str) -> Callable[[SourcePack, str], SourcePack]:
    def _value(value: str) -> dict[str, Any]:
        if setting == "max_pages":
            return {"max_pages": int(value)}
        if setting == "hostname_keywords":
            return {"hostname_keywords": None if value == "none" else value}
        return {"pivot_scope": value}

    def _apply(pack: SourcePack, value: str) -> SourcePack:
        return dataclasses.replace(
            pack,
            sources=tuple(
                dataclasses.replace(s, **_value(value)) if s.name == source_name else s for s in pack.sources
            ),
        )

    return _apply


# ── Duplicating a built-in pack ──────────────────────────────────────────────


def _spec_of(source: SourceDefinition, step_key: str, pivot_key: str | None) -> dict[str, Any] | None:
    """A built-in source as a builder source, or None for one it cannot hold."""
    from app.services.investigation_packs import ACTION_MAP_INFRASTRUCTURE, ACTION_PIVOT_NEIGHBOURS

    spec: dict[str, Any] = {"subject_types": list(source.subject_types)}
    if source.kind == "opencti" and not source.opencti_values and source.subject is None:
        return {**spec, "kind": "opencti", "name": source.name}
    if source.kind == "site_fingerprint":
        return {**spec, "kind": "site_fingerprint", "name": source.name, "fingerprint_set": source.fingerprint_set}
    if source.kind == "http" and REFERENCE_SOURCES.get(source.name) is not None:
        return {**spec, "kind": "reference", "reference": source.name}
    if source.kind == "web_search":
        return {**spec, "kind": "web_search", "name": source.name, "search_prompt": source.search_prompt}
    if source.kind == "passive_dns" and source.subject is None:
        out: dict[str, Any] = {**spec, "kind": "passive_dns", "name": source.name}
        if source.hostname_keywords:
            out["hostname_keywords"] = source.hostname_keywords
        if source.max_pages is not None:
            out["max_pages"] = source.max_pages
        if source.action == ACTION_MAP_INFRASTRUCTURE and pivot_key:
            out["pivot_step"] = pivot_key
        elif source.action == ACTION_PIVOT_NEIGHBOURS:
            out["pivot_step"] = step_key
        return out
    return None


def definition_from_builtin(pack: SourcePack) -> dict[str, Any]:
    """A built-in pack as builder fields (`PackFields`), to duplicate and edit.

    Its plan's actions become steps, in the order of the path for its first
    subject type; an action fed by another's hops (the pivot) keeps them,
    an action left with no source and no hop into it is dropped: its work
    happens in the step its hops serve. A pack without a plan is one step.
    """
    from app.core.utils import fit_copy_name
    from app.models.investigation_pack import InvestigationPack
    from app.services.investigation_packs import ACTION_MAP_INFRASTRUCTURE

    def _key(slug: str) -> str:
        return re.sub(r"[^a-z0-9_-]", "-", slug.lower())[:40] or "step"

    steps: list[dict[str, Any]] = []
    if pack.goal is None:
        sources = [spec for s in pack.sources if (spec := _spec_of(s, "sources", None))]
        steps.append({"key": "sources", "label": "Read the sources", "icon": "globe", "sources": sources})
    else:
        initial = pack.goal.initial_state(pack.subject_types[0])
        path = [a for a in (pack.goal.plan(initial) or pack.goal.actions) if not a.produces_report]
        fed = {a.fed_by: a.slug for a in path if a.fed_by}
        for action in path:
            key = _key(action.slug)
            pivot = fed.get(action.slug) if action.slug == ACTION_MAP_INFRASTRUCTURE else None
            sources = [
                spec
                for s in pack.sources
                if s.action == action.slug and (spec := _spec_of(s, key, _key(pivot) if pivot else None))
            ]
            sources += [
                {
                    "kind": "knowledge_base",
                    "kb_slug": k.slug,
                    "subject_types": list(k.subject_types),
                    **({"query_template": k.query_template} if k.query_template else {}),
                    **({"min_similarity": k.min_similarity} if k.min_similarity is not None else {}),
                }
                for k in pack.knowledge_bases
                if k.action == action.slug and k.slug
            ]
            steps.append(
                {
                    "key": key,
                    "label": action.label,
                    "description": action.description,
                    "icon": action.icon,
                    "sources": sources[:MAX_STEP_SOURCES],
                }
            )
        pivots = {src.get("pivot_step") for step in steps for src in step["sources"]}
        steps = [step for step in steps if step["sources"] or step["key"] in pivots][:MAX_STEPS]
    return {
        "name": fit_copy_name(pack.label, InvestigationPack.__table__.c.name, suffix=" (copy)"),
        "description": pack.description,
        "subject_types": list(pack.subject_types),
        "recommended": False,
        "max_iterations": min(pack.max_iterations, MAX_ITERATIONS_CEILING),
        "definition": {"steps": steps, "exposed": []},
    }


# ── What the builder offers ─────────────────────────────────────────────────

#: How each kind of source reads in the builder (translated in the frontend catalogs).
KIND_DESCRIPTIONS = {
    "opencti": "What the registered OpenCTI already knows, read as the runner.",
    "passive_dns": "VirusTotal passive DNS, with the runner's own VirusTotal; may pivot into a step.",
    "site_fingerprint": "Fetch the site's home page and test a set of signals.",
    "web_search": "A cited web search, with the runner's search provider.",
    "knowledge_base": "A knowledge base the runner can see, searched for the subject.",
    "mcp_tool": "An MCP server's designated investigation tool, if the runner can see the server.",
    "reference": "A public reference lookup (RDAP, certificates, Shodan InternetDB, NVD, ...).",
    "http": "A URL you type, with a placeholder for the subject: read as text, checked each time it is fetched.",
}


def builder_catalog() -> dict[str, Any]:
    """Everything the builder page offers, from the server: the frontend names
    no source, keyword set or icon of its own."""
    from app.services.investigation_packs import FINGERPRINT_SETS, SUBJECT_TYPES

    return {
        "subject_types": list(SUBJECT_TYPES),
        "kinds": [
            {"kind": kind, "label": _KIND_NAMES[kind], "description": KIND_DESCRIPTIONS[kind], "subject_types": list(types)}
            for kind, types in KIND_SUBJECT_TYPES.items()
        ],
        "reference_sources": [
            {"name": name, "subject_types": list(source.subject_types)} for name, source in REFERENCE_SOURCES.items()
        ],
        "hostname_keywords": [{"value": k.slug, "label": k.label} for k in HOSTNAME_KEYWORD_SETS.values()],
        "fingerprint_sets": [{"value": f.slug, "label": f.label} for f in FINGERPRINT_SETS.values()],
        "icons": sorted(GOAL_ACTION_ICONS),
        "settings": [
            {
                "setting": setting,
                "label": _SETTING_LABELS[setting],
                "choices": [{"value": c.value, "label": c.label} for c in _SETTING_CHOICES[setting]],
            }
            for setting in ("pivot_scope", "max_pages", "hostname_keywords")
        ],
        "limits": {
            "steps": MAX_STEPS,
            "sources_per_step": MAX_STEP_SOURCES,
            "exposed_settings": MAX_EXPOSED_SETTINGS,
            "max_iterations": MAX_ITERATIONS_CEILING,
        },
    }
