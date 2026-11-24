/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertCircle, ArrowLeftRight, BookOpen, Brain, Check, CheckCircle2,
  ChevronLeft, ChevronRight, CircleUser, Cog, Cpu, GraduationCap, Lightbulb,
  MessageSquareText, Shield, Sparkles, Wallet, Wrench, X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { BackButton } from "@/components/ui/back-button";
import { IconPicker } from "@/components/ui/icon-picker";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { TemplateGallery } from "@/components/ui/template-gallery";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { StepBar } from "@/components/ui/step-bar";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { CatalogRefreshNotice } from "@/pages/flow-builder/CatalogRefreshNotice";
import { MultiSelectListField } from "@/pages/flow-builder/MultiSelectListField";
import { WizardCanvas, type WizardNodeKey } from "@/pages/new-agent-wizard/WizardCanvas";
import { ProviderLogo } from "@/pages/new-agent-wizard/ProviderLogo";
import { PromptLibraryList } from "@/pages/new-agent-wizard/PromptLibraryList";
import { NewKnowledgeBaseInline } from "@/pages/new-agent-wizard/NewKnowledgeBaseInline";
import {
  AssistFrame, WizardAssistantBar, type WizardAssistTurn,
} from "@/pages/new-agent-wizard/WizardAssistant";
import {
  type AssistTarget, type WizardStepKey, freshDraft, planAssist, toAssistDraft,
} from "@/pages/new-agent-wizard/assist-plan";
import { useAssistPlayback } from "@/pages/new-agent-wizard/useAssistPlayback";
import { assistErrorLabel, assistNoteLabel } from "@/pages/new-agent-wizard/assist-notes";
import {
  type WizardDraft, WIZARD_DEFAULT_ICON, browserDraftStorage, clearWizardDraft, hasWizardDraftContent,
  loadWizardDraft, resolveDraftRestore, saveWizardDraft, withoutUnavailable, wizardDraftOwner,
} from "@/pages/new-agent-wizard/draft-storage";
import { resolveOptionLabel } from "@/pages/new-agent-wizard/option-labels";
import { useAgentTemplates, useAssistAgentWizard, useCreateAgent } from "@/hooks/api/useAgentApi";
import { putRoiDeclaration, useRoiSuggestion } from "@/hooks/api/useRoiDeclarationApi";
import { RoiSuggestionRow } from "@/components/roi/RoiSuggestionRow";
import { Label } from "@/components/ui/label";
import { suggestionDeclaration } from "@/components/roi/roi-outcomes";
import { useDebounce } from "@/hooks/useDebounce";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import { useMe } from "@/hooks/api/useAuthApi";
import { useIntegrations, useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { useFlowNodeCatalog } from "@/hooks/api/useFlowApi";
import { AgenticGatePage } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { BASE_PATH } from "@/lib/base-path";
import { cn } from "@/lib/utils";
import { ICON_MAP, INTEGRATION_TYPE_LABELS } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { AgentTemplate, FlowFieldOption, TemplateSetupRequirement } from "@/types";

type StepKey = WizardStepKey;

interface WizardStep {
  key: StepKey;
  title: string;
  icon: typeof Wrench;
  instruction: string;
  /** The embedded tutorial shown by the step's Tutorial button. */
  docs: string[];
  highlight: WizardNodeKey[];
}

/** The linear walkthrough, in order; the last entry is the review hub. */
const WALKTHROUGH_STEPS: WizardStep[] = [
  {
    key: "identity",
    title: "Identity",
    icon: CircleUser,
    instruction: "Give your agent a name people will recognize, an icon, and a one-line description of what it does.",
    docs: [
      "The name and icon are how the agent appears everywhere on the platform: the agents list, the chat picker, run traces and audit entries. Pick a name that says what the agent is for, not how it works.",
      "The description is a one-liner shown in lists and pickers. It helps teammates decide whether this agent is the right one to ask: keep it about the outcome the agent delivers.",
    ],
    highlight: ["agent"],
  },
  {
    key: "persona",
    title: "Persona",
    icon: MessageSquareText,
    instruction: "The persona is the agent's system prompt: who it is, how it behaves, what it should and should not do. You can generate a first draft with AI.",
    docs: [
      "The persona is the agent's system prompt: the standing instructions every conversation and run starts from. Write it in the second person (\"You are…\"), state the agent's scope, its tone, and what it must never do.",
      "Be explicit about boundaries: tools it should prefer, subjects to refuse, output expectations. A specific persona beats a long one.",
      "The AI generator can draft a first version from a plain-language description; refine it afterwards rather than starting from a blank page.",
      "You can also start from the platform's prompt library: any prompt from the Prompts collection can serve as this agent's persona.",
    ],
    highlight: ["agent"],
  },
  {
    key: "tools",
    title: "Tools",
    icon: Wrench,
    instruction: "Pick the tools the agent can call. You can refine this after creation.",
    docs: [
      "Tools are the actions the agent can take beyond writing text. Built-in tools ship with the platform (web fetch, file generation, code interpreter…); custom tools are JavaScript functions defined under Tools & Integrations.",
      "Only give the agent the tools its job needs: every tool is surface the agent can act through. If you pick nothing here, the agent starts with the platform's default tool set, and everything stays editable on the agent's page after creation.",
    ],
    highlight: ["tools"],
  },
  {
    key: "integrations",
    title: "Integrations",
    icon: Cog,
    instruction: "Bind the connected integrations the agent will act through: email, ticketing, repositories… Connect new ones from Tools & Integrations.",
    docs: [
      "An integration is a connected external service (Microsoft 365, GitHub, Jira, OpenCTI…) with its credentials. Binding one gives the agent that service's tools (reading mail, commenting on issues, querying a platform), using the connection's access.",
      "Only integrations already connected on this platform can be bound here. To add a new service, connect it first under Tools & Integrations, then bind it to the agent.",
      "A template can require an integration of a given type (for example email): the requirement stays amber until you bind a matching connection.",
    ],
    highlight: ["integrations"],
  },
  {
    key: "knowledge",
    title: "Knowledge bases",
    icon: BookOpen,
    instruction: "Bind the knowledge bases the agent should search when answering. Skip this if it does not need retrieval.",
    docs: [
      "A knowledge base is a searchable collection of documents. When bound, the agent retrieves relevant passages from it while answering: grounding its responses in your content instead of only its training.",
      "Bind the collections whose content the agent should rely on; leave this empty for agents that act on live systems rather than documents. Knowledge bases are created and filled under the Knowledge section.",
    ],
    highlight: ["knowledge"],
  },
  {
    key: "review",
    title: "Review & create",
    icon: Check,
    instruction: "Everything is ready: create the agent now, or refine an advanced option below first.",
    docs: [
      "Creating the agent persists everything configured in the previous steps in one operation. If you started from a template, its pre-configured assignments are created with it.",
      "Nothing here is final: every section (persona, tools, integrations, knowledge, and the advanced options below) stays editable on the agent's page afterwards.",
    ],
    highlight: ["agent"],
  },
];

/** Optional refinements, reachable from the review hub only — each is a
 *  guided step like the walkthrough ones, with its own tutorial. */
const ADVANCED_STEPS: WizardStep[] = [
  {
    key: "model",
    title: "Model",
    icon: Cpu,
    instruction: "Pin the LLM this agent runs on, or keep the platform default.",
    docs: [
      "By default the agent runs on the platform's default chat model, chosen by your admin, and follows it when that default changes. Pin a model only when this agent needs specific capabilities: a stronger reasoner, a cheaper model for high-volume work.",
      "The list shows the models published by the LLM providers configured on this platform. Temperature, reasoning effort and failover stay tunable on the agent's page.",
    ],
    highlight: ["agent"],
  },
  {
    key: "memory",
    title: "Memory",
    icon: Brain,
    instruction: "Choose which memory tiers the agent keeps. All four are on by default.",
    docs: [
      "Short-term memory is the running conversation; medium-term carries context across recent sessions; persistent memory stores durable facts the agent learns; user memory remembers preferences per user.",
      "Turn tiers off for agents that must not retain anything between runs: compliance-sensitive workflows, or stateless single-shot generators.",
    ],
    highlight: ["memory"],
  },
  {
    key: "guardrails",
    title: "Guardrails",
    icon: Shield,
    instruction: "Hard rules the agent must never break, checked on top of the persona.",
    docs: [
      "Guardrails are standing constraints appended with high priority to every run, things the agent must never do regardless of what a conversation asks: never disclose internal hostnames, never contact customers directly, never delete data.",
      "Keep them short and absolute. Behavioural style belongs in the persona; guardrails are for the lines that must not be crossed.",
    ],
    highlight: ["guardrails"],
  },
  {
    key: "skills",
    title: "Skills",
    icon: Lightbulb,
    instruction: "Attach skills: packaged instructions the agent loads when a task calls for them.",
    docs: [
      "A skill is a packaged instruction set (a markdown playbook, with optional resources) for one kind of task: drafting a weekly report, converting data between formats, writing in a house style. Skills live under Agent studio > Skills.",
      "Attached skills appear to the agent as a catalog; it loads a skill's full instructions only when the task calls for it, so attaching several stays cheap.",
    ],
    highlight: ["skills"],
  },
  {
    key: "handovers",
    title: "Handovers",
    icon: ArrowLeftRight,
    instruction: "Let this agent hand a conversation over to other agents when they are better placed.",
    docs: [
      "A handover lets this agent transfer a conversation (or consult) to another agent: a specialist for a sub-domain, an escalation path to a supervisor agent. Pick the agents this one may hand over to.",
      "Handover conditions and types (transfer vs consult) are refined on the agent's page; the platform's handover graph shows the resulting topology.",
    ],
    highlight: ["handovers"],
  },
  {
    key: "budget",
    title: "Budget",
    icon: Wallet,
    instruction: "Cap what a single run of this agent may spend. Empty fields inherit the platform limits.",
    docs: [
      "Budgets bound one run of the agent: money, tokens, tool calls and wall-clock time. When a run hits a limit, it wraps up with what it has instead of burning further.",
      "Every empty field inherits the platform-wide limit set by your admin: you only need values here when this agent should be tighter. Raising a limit above the platform ceiling requires an admin.",
    ],
    highlight: ["agent"],
  },
];

const STEPS: WizardStep[] = [...WALKTHROUGH_STEPS, ...ADVANCED_STEPS];
const REVIEW_INDEX = WALKTHROUGH_STEPS.length - 1;
const STEP_INDEX_BY_KEY = Object.fromEntries(STEPS.map((s, i) => [s.key, i])) as Record<StepKey, number>;
const modelChoiceOf = (o: { value: string; provider_id?: string | null }) =>
  `${o.provider_id ?? ""}::${o.value}`;

/**
 * Guided agent creation at /agents/new/wizard.
 *
 * Template gallery first, then a step-by-step assisted build: a side panel
 * carries the current step's instruction and fields, while a display-only
 * canvas mirrors the draft and highlights the node the step concerns. Each
 * step offers an embedded tutorial, rendered as an overlay on the canvas.
 * The canvas is a visual construction support — the deliverable is the
 * agent, created in one POST at the end; nothing is persisted before that.
 *
 * Once the walkthrough has reached the review step, the wizard loosens into a
 * hub: Create first, then the ADVANCED options the walkthrough deliberately
 * skipped (model, memory, guardrails, skills, handovers, budget) — each a
 * guided step of its own that returns to the hub when done.
 */
export default function NewAgentWizard() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const agenticAIEnabled = useAgenticAIEnabled();
  const aiOff = useAiActionDisabled();
  const { data: templates, isLoading } = useAgentTemplates();
  const { data: integrations } = useIntegrations();
  const { data: providerCatalog } = useProviderCatalog("integration");
  const {
    data: nodeCatalog, dataUpdatedAt: nodeCatalogUpdatedAt, refetch: refetchNodeCatalog,
    isRefetchError: nodeCatalogStale, isFetching: nodeCatalogFetching,
  } = useFlowNodeCatalog();
  const create = useCreateAgent();

  const [template, setTemplate] = useState<AgentTemplate | null>(null);
  const [started, setStarted] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  // Once the walkthrough reached the review, navigation loosens into hub mode.
  const [reviewReached, setReviewReached] = useState(false);
  const [showDocs, setShowDocs] = useState(false);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState(WIZARD_DEFAULT_ICON);
  const [description, setDescription] = useState("");
  // The unit of work suggested from the draft name and description (#9836 R8b): accepted here,
  // declared once the agent exists. A token match over the catalog, no model call.
  const roiLocked = useEnterpriseLockedReason() !== null;
  const draftName = useDebounce(name, 400);
  const draftDescription = useDebounce(description, 400);
  const roiSuggest = useRoiSuggestion(roiLocked ? null : { name: draftName, description: draftDescription });
  const roiSuggestion = roiSuggest.data?.suggestion ?? null;
  const [acceptedRoiUnit, setAcceptedRoiUnit] = useState<string | null>(null);
  const roiAccepted = !!roiSuggestion && acceptedRoiUnit === roiSuggestion.unit;
  const [persona, setPersona] = useState("");
  // Connected integrations the agent is bound to (any, not only required types).
  const [integrationIds, setIntegrationIds] = useState<string[]>([]);
  // Catalog "tools" values: "builtin:<id>" for built-ins, custom tool ids.
  const [toolIds, setToolIds] = useState<string[]>([]);
  const [kbIds, setKbIds] = useState<string[]>([]);
  // Advanced options. Model is stored as "<provider_id>::<model_id>" because
  // model ids are not unique across providers; "" = platform default.
  const [modelChoice, setModelChoice] = useState("");
  const [memory, setMemory] = useState({ short: true, medium: true, persistent: true, user: true });
  const [guardrails, setGuardrails] = useState("");
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [handoverIds, setHandoverIds] = useState<string[]>([]);
  // Kept as strings for the inputs; parsed at creation. "" = inherit.
  const [budget, setBudget] = useState({ cost: "", tokens: "", calls: "", seconds: "" });

  useEffect(() => {
    if (stepIndex === REVIEW_INDEX) setReviewReached(true);
  }, [stepIndex]);

  /** Puts a draft's fields - not its template, step or progress - in the
   *  wizard: a restored draft, a template's starting point, the assistant's
   *  changes and their undo. */
  const applyFields = useCallback((d: WizardDraft) => {
    setName(d.name);
    setIcon(d.icon);
    setDescription(d.description);
    setPersona(d.persona);
    setIntegrationIds(d.integrationIds);
    setToolIds(d.toolIds);
    setKbIds(d.kbIds);
    setModelChoice(d.modelChoice);
    setMemory(d.memory);
    setGuardrails(d.guardrails);
    setSkillIds(d.skillIds);
    setHandoverIds(d.handoverIds);
    setBudget(d.budget);
  }, []);

  // In-app navigation (the sidebar, Back, the Integrations step's "Connect
  // one") unmounts the wizard, and a client-side transition cannot be blocked
  // without a data router. So the draft is kept per user in session storage
  // while it is dirty and restored when the user comes back; the
  // tab-close / refresh path keeps its beforeunload prompt below.
  const { data: me } = useMe();
  const draftOwner = me?.id ? wizardDraftOwner(String(me.id), BASE_PATH) : null;
  const [draftChecked, setDraftChecked] = useState(false);
  const [draftRestored, setDraftRestored] = useState(false);
  // The draft just restored, until a catalog read made after the restore has
  // confirmed that what it selected still exists.
  const [unconfirmedDraft, setUnconfirmedDraft] = useState<{ draft: WizardDraft; at: number } | null>(null);

  useEffect(() => {
    if (draftChecked || isLoading || !draftOwner) return;
    // A new draft was started while the stored one waited for its template:
    // the new one wins.
    if (started) {
      setDraftChecked(true);
      return;
    }
    const storage = browserDraftStorage();
    const draft = loadWizardDraft(storage, draftOwner);
    const restore = resolveDraftRestore(draft, templates);
    // The template list failed to load: the draft is kept, and rebuilt once
    // the list is back.
    if (restore.kind === "wait") return;
    setDraftChecked(true);
    if (restore.kind === "drop") clearWizardDraft(storage, draftOwner);
    if (restore.kind !== "restore" || !draft) return;
    setTemplate(restore.template);
    applyFields(draft);
    setStepIndex(Math.min(draft.stepIndex, STEPS.length - 1));
    setReviewReached(draft.reviewReached);
    setStarted(true);
    setDraftRestored(true);
    // Something it selected may have been deleted or unshared meanwhile, and
    // the cached catalog can be minutes old: read it again.
    setUnconfirmedDraft({ draft, at: Date.now() });
    void refetchNodeCatalog();
  }, [draftChecked, isLoading, draftOwner, templates, started, refetchNodeCatalog, applyFields]);

  useEffect(() => {
    if (!unconfirmedDraft || !nodeCatalog || nodeCatalogUpdatedAt < unconfirmedDraft.at) return;
    const { draft } = unconfirmedDraft;
    setUnconfirmedDraft(null);
    const sources = nodeCatalog.sources;
    setToolIds((prev) => withoutUnavailable(prev, draft.toolIds, sources?.tools || []));
    setKbIds((prev) => withoutUnavailable(prev, draft.kbIds, sources?.knowledge_bases || []));
    setSkillIds((prev) => withoutUnavailable(prev, draft.skillIds, sources?.skills || []));
    setHandoverIds((prev) => withoutUnavailable(prev, draft.handoverIds, sources?.agents || []));
    const models = (sources?.models || []).map(modelChoiceOf);
    setModelChoice((prev) => (prev && prev === draft.modelChoice && !models.includes(prev) ? "" : prev));
  }, [unconfirmedDraft, nodeCatalog, nodeCatalogUpdatedAt]);

  const currentDraft = useMemo<WizardDraft>(() => ({
    templateSlug: template?.slug ?? null,
    stepIndex,
    reviewReached,
    name,
    icon,
    description,
    persona,
    integrationIds,
    toolIds,
    kbIds,
    modelChoice,
    memory,
    guardrails,
    skillIds,
    handoverIds,
    budget,
  }), [
    template, stepIndex, reviewReached, name, icon, description, persona, integrationIds, toolIds,
    kbIds, modelChoice, memory, guardrails, skillIds, handoverIds, budget,
  ]);

  // ── The assistant (AGENT_WIZARD_ASSIST) ──
  const assistEnabled = useFeatureFlag("AGENT_WIZARD_ASSIST");
  const assist = useAssistAgentWizard();
  const [assistTurns, setAssistTurns] = useState<WizardAssistTurn[]>([]);
  // What Undo puts back: the wizard as it was when the last answer landed.
  const [assistUndo, setAssistUndo] = useState<{ draft: WizardDraft; started: boolean; template: AgentTemplate | null } | null>(null);
  // Bumped by every send and by Stop: an answer to an older send is dropped.
  const assistSeq = useRef(0);
  // Aborts the request in flight: Stop, a newer send, leaving the wizard.
  const assistAbort = useRef<AbortController | null>(null);
  useEffect(() => () => assistAbort.current?.abort(), []);
  // The draft as the wizard holds it now, for the player: an operation applies
  // to what the person sees, their own edits made meanwhile included.
  const latestDraft = useRef(currentDraft);
  useEffect(() => {
    latestDraft.current = currentDraft;
  }, [currentDraft]);
  const startedRef = useRef(started);
  useEffect(() => {
    startedRef.current = started;
  }, [started]);
  const playback = useAssistPlayback({
    getDraft: useCallback(() => latestDraft.current, []),
    setDraft: useCallback((d: WizardDraft) => {
      latestDraft.current = d;
      applyFields(d);
    }, [applyFields]),
    openStep: useCallback((key: WizardStepKey) => setStepIndex(STEP_INDEX_BY_KEY[key]), []),
  });
  const assistBusy = assist.isPending || playback.playing;
  const assistFocus = (target: AssistTarget) => playback.focus?.target === target;

  const dirty = started && !create.isSuccess && hasWizardDraftContent(currentDraft);

  // Not kept while the create request is in flight: a reload or a navigation
  // before its success handler runs would otherwise bring back the draft of
  // an agent that already exists. A failed create makes it dirty again.
  const keepDraft = dirty && !create.isPending;

  // Written on every change rather than debounced: the "Connect one" click
  // that leaves the wizard must never outrun the last keystroke.
  useEffect(() => {
    if (!draftChecked || !draftOwner) return;
    const storage = browserDraftStorage();
    if (!keepDraft) {
      // Created, abandoned for the gallery, or emptied: nothing to come back to.
      clearWizardDraft(storage, draftOwner);
      return;
    }
    saveWizardDraft(storage, draftOwner, currentDraft);
  }, [draftChecked, draftOwner, keepDraft, currentDraft]);

  const discardDraft = () => {
    if (draftOwner) clearWizardDraft(browserDraftStorage(), draftOwner);
    setDraftRestored(false);
    setUnconfirmedDraft(null);
    setTemplate(null);
    setStarted(false);
  };

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Safari still requires returnValue for the dialog to actually show.
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (!agenticAIEnabled || aiOff) return <AgenticGatePage feature="Creating agents" />;
  if (isLoading) return <PageSkeleton cards={6} />;

  const requirements: TemplateSetupRequirement[] = template?.must_be_defined || [];
  const integrationRequirements = requirements.filter((r) => r.kind === "integration");
  const knowledgeRequirements = requirements.filter((r) => r.kind === "knowledge_base");
  const personaRequirements = requirements.filter((r) => r.kind === "persona_field");

  const connected = integrations?.filter((i) => i.status === "connected") || [];
  const providersForType = (type: string) =>
    providerCatalog?.filter((c) => c.integration_types.includes(type)) || [];
  const connectedForType = (type: string) => {
    const keys = providersForType(type).map((p) => p.provider);
    return connected.filter((i) => keys.includes(i.provider));
  };

  // Types the template asks about, rendered as their own sections; every other
  // connected integration is offered below them.
  const requiredTypes = [
    ...new Set([
      ...(template?.required_integration_types || []),
      ...integrationRequirements.map((r) => r.target),
    ]),
  ];
  const typedIntegrationIds = new Set(
    requiredTypes.flatMap((type) => connectedForType(type).map((i) => i.id)),
  );
  const otherConnected = connected.filter((i) => !typedIntegrationIds.has(i.id));

  const integrationRequirementMet = (r: TemplateSetupRequirement) => {
    const serving = connectedForType(r.target).map((i) => i.id);
    return integrationIds.some((id) => serving.includes(id));
  };

  const toolOptions = nodeCatalog?.sources?.tools || [];
  const kbOptions = nodeCatalog?.sources?.knowledge_bases || [];
  const skillOptions = nodeCatalog?.sources?.skills || [];
  const agentOptions = nodeCatalog?.sources?.agents || [];
  const modelOptions = nodeCatalog?.sources?.models || [];
  const chosenModel = modelOptions.find((o) => modelChoiceOf(o) === modelChoice);

  // Once the catalog has loaded, a selection it does not list is no longer
  // available to the user (deleted, unshared, restored from an older draft):
  // the canvas names it as such rather than showing its id, and its picker
  // offers it for removal - creating the agent with it would be refused.
  const catalogLoaded = nodeCatalog !== undefined;
  const unavailableLabel = catalogLoaded ? t_i18n("Unavailable") : undefined;
  const isAvailable = (options: FlowFieldOption[], id: string) =>
    !catalogLoaded || options.some((o) => o.value === id);
  // Before the catalog's first load lands there is nothing to list yet, which
  // an empty picker would present as nothing being available.
  const catalogPicker = (options: FlowFieldOption[], selected: string[], onChange: (value: string[]) => void) =>
    catalogLoaded ? (
      <MultiSelectListField options={options} value={selected} onChange={onChange} showUnlisted />
    ) : (
      <p className="rounded-sm border border-elevation-subtle-soft px-3 py-2 content-compact text-default-secondary">
        {t_i18n("Loading...")}
      </p>
    );

  // A knowledge_base requirement whose target matches a selectable KB must be
  // satisfied by THAT KB; a target this instance cannot resolve falls back to
  // "any available KB selected" rather than wedging the step.
  const kbRequirementMet = (r: TemplateSetupRequirement) => {
    const targeted = kbOptions.find((o) => o.value === r.target || o.label === r.target);
    return targeted ? kbIds.includes(targeted.value) : kbIds.some((id) => isAvailable(kbOptions, id));
  };

  const step = STEPS[stepIndex];
  const isAdvancedStep = stepIndex > REVIEW_INDEX;
  const mandatoryDone: Partial<Record<StepKey, boolean>> = {
    identity: !!name.trim(),
    persona: !!persona.trim(),
    integrations: integrationRequirements.every(integrationRequirementMet),
    knowledge: knowledgeRequirements.every(kbRequirementMet),
  };
  const canAdvance = mandatoryDone[step.key] ?? true;
  // Hub mode unlocks free navigation, so Next's per-step gating is bypassable:
  // creation itself must re-check every mandatory gate.
  const incompleteSteps = WALKTHROUGH_STEPS.filter(
    (s) => (mandatoryDone[s.key] ?? true) === false,
  );
  const allMandatoryDone = incompleteSteps.length === 0;

  const toggleIntegration = (id: string) =>
    setIntegrationIds((prev) => (prev.includes(id) ? prev.filter((v) => v !== id) : [...prev, id]));

  const handleSelectTemplate = (tmpl: AgentTemplate | null) => {
    setTemplate(tmpl);
    // Every selection is a fresh starting point: never carry a previous
    // template's identity — or, worse, its integration bindings, which would
    // no longer be rendered yet still be granted at creation — into the new
    // draft.
    applyFields(freshDraft(tmpl, WIZARD_DEFAULT_ICON));
    setStepIndex(0);
    setReviewReached(false);
    setShowDocs(false);
    setDraftRestored(false);
    setUnconfirmedDraft(null);
    setStarted(true);
  };

  const sendToAssistant = (instruction: string) => {
    const seq = ++assistSeq.current;
    assistAbort.current?.abort();
    const abort = new AbortController();
    assistAbort.current = abort;
    // Refusals and left-out notes are said to the person, never replayed to
    // the model as conversation; the route takes the last 20 turns at most.
    const history = assistTurns
      .filter((t) => !t.notice)
      .slice(-20)
      .map(({ role, content }) => ({ role, content }));
    setAssistTurns((t) => [...t, { role: "user", content: instruction }]);
    assist.mutate(
      {
        instruction,
        draft: toAssistDraft(latestDraft.current, step.key),
        started,
        history,
        icon_names: Object.keys(ICON_MAP),
        signal: abort.signal,
      },
      {
        onSuccess: ({ reply, patch, notes }) => {
          if (seq !== assistSeq.current) return;
          // What Undo puts back: the wizard as the answer finds it, edits the
          // person made while it was on its way included.
          const before = {
            draft: latestDraft.current,
            started: startedRef.current,
            template: templates?.find((t) => t.slug === latestDraft.current.templateSlug) ?? null,
          };
          setAssistTurns((t) => [
            ...t,
            { role: "assistant", content: reply },
            ...notes.map((code): WizardAssistTurn => ({
              role: "assistant",
              content: t_i18n(assistNoteLabel(code)),
              notice: true,
            })),
          ]);
          let base = latestDraft.current;
          // Still on the gallery (the person did not pick a template while the
          // answer was on its way): the assistant's starting point opens the
          // wizard, and its changes play on top of it.
          if (!startedRef.current) {
            const tmpl = patch.template_slug ? (templates?.find((t) => t.slug === patch.template_slug) ?? null) : null;
            handleSelectTemplate(tmpl);
            base = freshDraft(tmpl, WIZARD_DEFAULT_ICON);
            latestDraft.current = base;
          }
          setAssistUndo(before);
          void playback.play(planAssist(base, patch));
        },
        onError: (error) => {
          if (seq !== assistSeq.current) return;
          const label = assistErrorLabel(error);
          const content = label
            ? t_i18n(label)
            : (error instanceof Error && error.message) || t_i18n("The assistant could not prepare a change.");
          setAssistTurns((t) => [...t, { role: "assistant", notice: true, content }]);
        },
      },
    );
  };

  const stopAssistant = () => {
    if (assist.isPending || playback.playing) {
      setAssistTurns((t) => [...t, { role: "assistant", notice: true, content: t_i18n("Stopped.") }]);
    }
    assistSeq.current++;
    assistAbort.current?.abort();
    playback.stop();
  };

  const undoAssistant = () => {
    if (!assistUndo) return;
    playback.stop();
    setTemplate(assistUndo.template);
    applyFields(assistUndo.draft);
    setStepIndex(assistUndo.draft.stepIndex);
    setReviewReached(assistUndo.draft.reviewReached);
    // An answer that opened the wizard from the gallery is undone back to it.
    setStarted(assistUndo.started);
    setAssistUndo(null);
  };

  const handleCreate = () => {
    const integrationBindings: Record<string, string[]> = {};
    for (const integrationId of integrationIds) {
      const integ = connected.find((i) => i.id === integrationId);
      if (integ) integrationBindings[integ.provider] = [...(integrationBindings[integ.provider] || []), integrationId];
    }
    const builtinIds = toolIds.filter((v) => v.startsWith("builtin:")).map((v) => v.slice("builtin:".length));
    const customIds = toolIds.filter((v) => !v.startsWith("builtin:"));
    // min="0" on a number input does not constrain typed input: sanitize per
    // field so "abc" or a negative never reaches the POST as NaN/null.
    const num = (v: string) => {
      const n = Number(v);
      return v !== "" && Number.isFinite(n) && n >= 0 ? n : undefined;
    };
    const budgetConfig = {
      max_cost_usd: num(budget.cost),
      max_total_tokens: num(budget.tokens),
      max_tool_calls: num(budget.calls),
      max_duration_seconds: num(budget.seconds),
    };
    const hasBudget = Object.values(budgetConfig).some((v) => v !== undefined);

    // Before the request, not after: isPending only reaches the save effect
    // on the next render.
    if (draftOwner) clearWizardDraft(browserDraftStorage(), draftOwner);
    create.mutate(
      {
        template_slug: template?.slug,
        name,
        icon,
        description: description || undefined,
        persona: persona || undefined,
        disable_chat: template?.disable_chat || undefined,
        create_default_assignments: !!template,
        integration_bindings: Object.keys(integrationBindings).length > 0 ? integrationBindings : undefined,
        // Only override the platform defaults when the user picked something.
        builtin_tool_ids: builtinIds.length > 0 ? builtinIds : undefined,
        custom_tool_ids: customIds.length > 0 ? customIds : undefined,
        knowledge_base_ids: kbIds.length > 0 ? kbIds : undefined,
        llm_model: chosenModel?.value || undefined,
        llm_provider_id: chosenModel?.provider_id || undefined,
        memory_short_term_enabled: memory.short,
        memory_medium_term_enabled: memory.medium,
        memory_persistent_enabled: memory.persistent,
        memory_user_enabled: memory.user,
        guardrails: guardrails.trim() || undefined,
        // Sent even empty when a template has defaults: an omitted list would
        // give the agent the template's default skills the user removed.
        skill_ids: skillIds.length > 0 || template?.default_skills?.length ? skillIds : undefined,
        handover_agent_ids: handoverIds.length > 0 ? handoverIds : undefined,
        budget_config: hasBudget ? budgetConfig : undefined,
      },
      {
        // On failure the global mutation-cache toast reports the backend's
        // message and the user stays on the hub, free to open the section that
        // owns the rejected field.
        onSuccess: (agent) => {
          // Cleared here, not left to the save effect: navigating away can
          // unmount the wizard before that effect sees the success.
          if (draftOwner) clearWizardDraft(browserDraftStorage(), draftOwner);
          toast({ title: t_i18n("Agent created"), variant: "success" });
          // The accepted suggestion is declared on the new agent; a refusal leaves the agent
          // created and says so: its Outcomes card offers the suggestion again.
          if (roiAccepted && roiSuggestion) {
            putRoiDeclaration({ type: "agent", id: agent.id }, suggestionDeclaration(roiSuggestion)).catch(() =>
              toast({ title: t_i18n("The suggested unit could not be saved. Pick a unit instead."), variant: "error" }),
            );
          }
          navigate(`/agents/${agent.id}`);
        },
      },
    );
  };

  const focusedStep = playback.focus ? STEPS[STEP_INDEX_BY_KEY[playback.focus.step]] : null;
  const assistantBar = assistEnabled ? (
    <WizardAssistantBar
      turns={assistTurns}
      pending={assist.isPending}
      playing={playback.playing}
      status={focusedStep ? t_i18n("Filling in {step}...", { step: t_i18n(focusedStep.title) }) : null}
      started={started}
      disabled={aiOff}
      canUndo={assistUndo !== null}
      onSend={sendToAssistant}
      onStop={stopAssistant}
      onUndo={undoAssistant}
    />
  ) : null;

  if (!started) {
    return (
      <div className={cn("space-y-6", assistantBar && "pb-4")}>
        <BackButton to="/agents/new" label="Back to Creation Modes" />
        <div>
          <h1 className="text-2xl font-bold">{t_i18n("Create New Agent")}</h1>
          <p className="mt-2 text-default-secondary">{t_i18n("Pick a starting point: the wizard walks you through the rest, step by step.")}</p>
        </div>
        <TemplateGallery templates={templates} onSelect={handleSelectTemplate} />
        {/* Describing the agent is the other way to start: the assistant
            picks the starting point and opens the wizard on it. */}
        {assistantBar && <div className="sticky bottom-4 z-20 mx-auto max-w-3xl">{assistantBar}</div>}
      </div>
    );
  }

  const requirementRow = (r: TemplateSetupRequirement, met: boolean) => (
    <div key={`${r.kind}:${r.target}`} className={cn("flex items-start gap-2 rounded-sm p-2.5 content-compact", met ? "bg-feedback-success-primary/5 text-default-secondary" : "bg-warning-box text-feedback-warning-tertiary")}>
      {met ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-success-primary" /> : <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
      <span>{r.instruction}</span>
    </div>
  );

  const integrationChip = (integ: { id: string; provider: string; name: string }) => (
    <button
      key={integ.id}
      type="button"
      className={cn(
        "flex items-center gap-2 rounded-sm border px-3 py-2 content-compact font-medium transition-colors",
        integrationIds.includes(integ.id) ? "border-accent/40 bg-accent/10 text-accent" : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:border-elevation-subtle",
      )}
      onClick={() => toggleIntegration(integ.id)}
    >
      <ProviderLogo provider={integ.provider} className="h-4 w-4" />
      {integ.name}
    </button>
  );

  const memoryRow = (key: keyof typeof memory, label: string) => (
    <div key={key} className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
      <span className="content-base">{t_i18n(label)}</span>
      <Switch aria-label={t_i18n(label)} checked={memory[key]} onCheckedChange={(on) => setMemory({ ...memory, [key]: on })} />
    </div>
  );

  const budgetField = (key: keyof typeof budget, label: string) => (
    <div key={key} className="space-y-1.5">
      <Label>{t_i18n(label)}</Label>
      <Input aria-label={t_i18n(label)}
        type="number"
        min="0"
        value={budget[key]}
        onChange={(e) => setBudget({ ...budget, [key]: e.target.value })}
        placeholder={t_i18n("Platform default")}
      />
    </div>
  );

  /** The review hub's advanced-option summaries. */
  const advancedSummary: Partial<Record<StepKey, string>> = {
    model: chosenModel?.label || t_i18n("Platform default"),
    memory: `${Object.values(memory).filter(Boolean).length}/4`,
    guardrails: guardrails.trim() ? t_i18n("Defined") : t_i18n("Not set"),
    skills: t_i18n("{count, plural, one {# skill} other {# skills}}", { count: skillIds.length }),
    handovers: t_i18n("{count, plural, one {# handover agent} other {# handover agents}}", { count: handoverIds.length }),
    budget: Object.values(budget).some((v) => v !== "") ? t_i18n("Custom limits") : t_i18n("Inherits platform limits"),
  };

  return (
    <div className="flex h-[calc(100vh-8rem)] min-h-[480px] gap-4">
      {/* ── Step panel — the guided instructions ── */}
      <div className="flex w-[380px] shrink-0 flex-col rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default">
        <div className="border-b border-elevation-subtle-soft p-4">
          <BackButton to="/agents/new" label="Back to Creation Modes" className="-ml-2 mb-2" />
          {draftRestored && (
            <div role="status" className="mb-3 flex items-center justify-between gap-2 rounded-sm bg-accent/5 px-2.5 py-1.5 content-compact text-default-secondary">
              <span>{t_i18n("Your unsaved draft was restored.")}</span>
              <Button variant="secondary" className="shrink-0" onClick={discardDraft}>
                {t_i18n("Discard draft")}
              </Button>
            </div>
          )}
          {nodeCatalogStale && (
            <CatalogRefreshNotice
              className="mb-3"
              retrying={nodeCatalogFetching}
              onRetry={() => void refetchNodeCatalog()}
            />
          )}
          {/* The shared step bar (UX r1 B1 on #1092): steps already passed stay reachable; skipping ahead unlocks once the walkthrough has reached the review. */}
          <StepBar
            ariaLabel={t_i18n("Steps")}
            steps={WALKTHROUGH_STEPS.map((s, i) => ({
              key: s.key,
              title: t_i18n(s.title),
              state: i === stepIndex ? "current" : i < stepIndex || reviewReached ? "settled" : "todo",
              disabled: !reviewReached && i > stepIndex,
            }))}
            onSelect={setStepIndex}
          />
          <p className="mt-3 content-compact font-medium text-default-secondary">
            {isAdvancedStep
              ? t_i18n("Advanced option")
              : t_i18n("Step {current} of {total}", { current: stepIndex + 1, total: WALKTHROUGH_STEPS.length })}
          </p>
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">{t_i18n(step.title)}</h2>
            <Button
              variant="ghost"
              size="sm"
              className={cn("gap-1.5 shrink-0", showDocs ? "text-accent hover:text-accent" : "text-default-secondary")}
              aria-pressed={showDocs}
              onClick={() => setShowDocs((v) => !v)}
            >
              <GraduationCap className="h-4 w-4" /> {t_i18n("Tutorial")}
            </Button>
          </div>
          <p className="mt-1 content-compact text-default-secondary">{t_i18n(step.instruction)}</p>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto p-4">
          {step.key === "identity" && (
            <>
              <AssistFrame active={assistFocus("name") || assistFocus("icon")} className="space-y-1.5">
                <Label htmlFor="wizard-agent-name">{t_i18n("Agent name")}</Label>
                <div className="flex items-center gap-2">
                  <IconPicker value={icon} onChange={setIcon} />
                  <Input id="wizard-agent-name" aria-label={t_i18n("Agent name")} className="flex-1" value={name} onChange={(e) => setName(e.target.value)} placeholder={t_i18n("e.g. Email Assistant")} autoFocus />
                </div>
              </AssistFrame>
              <AssistFrame active={assistFocus("description")} className="space-y-1.5">
                <Label htmlFor="wizard-agent-description">{t_i18n("Description (optional)")}</Label>
                <Input id="wizard-agent-description" aria-label={t_i18n("Description (optional)")} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t_i18n("What does this agent do?")} />
              </AssistFrame>
              {roiSuggestion && (
                <RoiSuggestionRow
                  suggestion={roiSuggestion}
                  accepted={roiAccepted}
                  onAccept={() => setAcceptedRoiUnit(roiSuggestion.unit)}
                  onUndo={() => setAcceptedRoiUnit(null)}
                  testId="wizard-roi-suggestion"
                />
              )}
            </>
          )}

          {step.key === "persona" && (
            <>
              {personaRequirements.map((r) => requirementRow(r, !!persona.trim()))}
              <AssistFrame active={assistFocus("persona")} className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label>{t_i18n("Persona / System Prompt")}</Label>
                  <PromptGenerator contextType="persona" currentText={persona} onGenerated={setPersona} agentName={name} />
                </div>
                <textarea
                  className="flex min-h-[220px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                  placeholder={t_i18n("Describe who you are and how the agent should behave...")}
                  value={persona}
                  onChange={(e) => setPersona(e.target.value)}
                />
              </AssistFrame>
              <div className="space-y-1.5">
                <Label>{t_i18n("Prompt library")}</Label>
                <p className="content-compact text-default-secondary">{t_i18n("Pick a prompt from the library to use as this agent's persona.")}</p>
                <PromptLibraryList value={persona} onPick={setPersona} />
              </div>
            </>
          )}

          {step.key === "tools" && (
            <AssistFrame active={assistFocus("tools")} className="space-y-1.5">
              <Label>{t_i18n("Tools the agent can call")}</Label>
              {catalogPicker(toolOptions, toolIds, setToolIds)}
            </AssistFrame>
          )}

          {step.key === "integrations" && (
            <AssistFrame active={assistFocus("integrations")} className="space-y-4">
              {integrationRequirements.map((r) => requirementRow(r, integrationRequirementMet(r)))}
              {requiredTypes.map((type) => {
                const options = connectedForType(type);
                const mandatory = integrationRequirements.some((r) => r.target === type);
                const met = options.some((i) => integrationIds.includes(i.id));
                return (
                  <div key={type} className="space-y-2 rounded-sm bg-elevation-hover p-3">
                    <div className="flex items-center gap-2">
                      <span className="content-compact font-medium">{t_i18n(INTEGRATION_TYPE_LABELS[type] || type)}</span>
                      {mandatory ? (
                        met
                          ? <CheckCircle2 className="h-3.5 w-3.5 text-feedback-success-primary" />
                          : <Badge variant="outline" className="text-[8px] text-feedback-warning-primary border-feedback-alert-primary/40">{t_i18n("required")}</Badge>
                      ) : (
                        <Badge variant="outline" className="text-[8px]">{t_i18n("optional")}</Badge>
                      )}
                    </div>
                    {options.length > 0 ? (
                      <div className="flex flex-wrap gap-2">{options.map(integrationChip)}</div>
                    ) : (
                      <div className="flex items-center gap-2 content-compact text-feedback-warning-primary">
                        <AlertCircle className="h-3.5 w-3.5" />
                        <span>{t_i18n("No connected {type} integration.", { type: (INTEGRATION_TYPE_LABELS[type] || type).toLowerCase() })} <button type="button" className="text-accent hover:underline" onClick={() => navigate("/integrations/new")}>{t_i18n("Connect one")}</button></span>
                      </div>
                    )}
                  </div>
                );
              })}
              {otherConnected.length > 0 && (
                <div className="space-y-2">
                  {requiredTypes.length > 0 && (
                    <Label>{t_i18n("Other connected integrations")}</Label>
                  )}
                  <div className="flex flex-wrap gap-2">{otherConnected.map(integrationChip)}</div>
                </div>
              )}
              {connected.length === 0 && requiredTypes.length === 0 && (
                <div className="flex items-center gap-2 content-compact text-default-secondary">
                  <AlertCircle className="h-3.5 w-3.5" />
                  <span>{t_i18n("No connected integrations yet.")} <button type="button" className="text-accent hover:underline" onClick={() => navigate("/integrations/new")}>{t_i18n("Connect one")}</button></span>
                </div>
              )}
            </AssistFrame>
          )}

          {step.key === "knowledge" && (
            <>
              {knowledgeRequirements.map((r) => requirementRow(r, kbRequirementMet(r)))}
              <AssistFrame active={assistFocus("knowledge")} className="space-y-1.5">
                <Label>{t_i18n("Knowledge bases to search")}</Label>
                {catalogPicker(kbOptions, kbIds, setKbIds)}
                <NewKnowledgeBaseInline
                  onCreated={(kb) => setKbIds((prev) => (prev.includes(kb.id) ? prev : [...prev, kb.id]))}
                  // A base created before the catalog has loaded could not be
                  // written into it: the picker would miss it and the canvas
                  // show its id until the restarted load lands.
                  disabledReason={catalogLoaded ? undefined : t_i18n("Loading knowledge bases...")}
                />
              </AssistFrame>
            </>
          )}

          {step.key === "model" && (
            <AssistFrame active={assistFocus("model")} className="space-y-1.5">
              <Label>{t_i18n("Model")}</Label>
              <SearchableSelect
                aria-label={t_i18n("Model")}
                options={modelOptions.map((o) => ({ value: modelChoiceOf(o), label: o.label, keywords: [o.value] }))}
                pinned={[{ value: "__default__", label: t_i18n("Platform default") }]}
                value={modelChoice || "__default__"}
                onValueChange={(v) => setModelChoice(v === "__default__" ? "" : v)}
                placeholder={t_i18n("Search models...")}
              />
            </AssistFrame>
          )}

          {step.key === "memory" && (
            <AssistFrame active={assistFocus("memory")} className="space-y-4">
              {memoryRow("short", "Short-term Memory")}
              {memoryRow("medium", "Medium-term Memory")}
              {memoryRow("persistent", "Persistent Memory")}
              {memoryRow("user", "User Memory")}
            </AssistFrame>
          )}

          {step.key === "guardrails" && (
            <AssistFrame active={assistFocus("guardrails")} className="space-y-1.5">
              <Label>{t_i18n("Guardrails")}</Label>
              <textarea
                className="flex min-h-[180px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                placeholder={t_i18n("e.g. Never disclose internal hostnames.")}
                value={guardrails}
                onChange={(e) => setGuardrails(e.target.value)}
              />
            </AssistFrame>
          )}

          {step.key === "skills" && (
            <AssistFrame active={assistFocus("skills")} className="space-y-1.5">
              <Label>{t_i18n("Skills")}</Label>
              {catalogPicker(skillOptions, skillIds, setSkillIds)}
            </AssistFrame>
          )}

          {step.key === "handovers" && (
            <AssistFrame active={assistFocus("handovers")} className="space-y-1.5">
              <Label>{t_i18n("Handover agents")}</Label>
              {catalogPicker(agentOptions, handoverIds, setHandoverIds)}
            </AssistFrame>
          )}

          {step.key === "budget" && (
            <AssistFrame active={assistFocus("budget")} className="space-y-4">
              {budgetField("cost", "Max cost (USD)")}
              {budgetField("tokens", "Max total tokens")}
              {budgetField("calls", "Max tool calls")}
              {budgetField("seconds", "Max duration (seconds)")}
            </AssistFrame>
          )}

          {step.key === "review" && (
            <div className="space-y-4">
              {/* Finish first; the optional refinements come below. */}
              <Button className="w-full" onClick={handleCreate} disabled={!allMandatoryDone || create.isPending || aiOff || assistBusy}>
                {create.isPending ? t_i18n("Creating...") : t_i18n("Create the agent")}
              </Button>
              {!allMandatoryDone && (
                <div className="flex items-start gap-2 rounded-sm bg-warning-box p-2.5 content-compact text-feedback-warning-tertiary">
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>
                    {t_i18n("Complete the required steps first: {steps}", {
                      steps: incompleteSteps.map((s) => t_i18n(s.title)).join(", "),
                    })}
                  </span>
                </div>
              )}
              {template && (
                <p className="flex items-center gap-2 content-compact text-default-secondary">
                  <Sparkles className="h-3.5 w-3.5 text-accent" />
                  {t_i18n("{count, plural, one {Includes # pre-configured assignment} other {Includes # pre-configured assignments}}", { count: template.assignment_count })}
                </p>
              )}
              <div className="space-y-2">
                <Label>{t_i18n("Advanced options")}</Label>
                {ADVANCED_STEPS.map((s) => {
                  const SIcon = s.icon;
                  return (
                    <button
                      key={s.key}
                      type="button"
                      className="flex w-full items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 text-left transition-colors hover:border-elevation-subtle hover:bg-elevation-highlight"
                      onClick={() => setStepIndex(STEP_INDEX_BY_KEY[s.key])}
                    >
                      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
                        <SIcon className="h-4 w-4 text-default-secondary" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="content-base font-medium">{t_i18n(s.title)}</p>
                        <p className="truncate content-compact text-default-secondary">{advancedSummary[s.key]}</p>
                      </div>
                      <ChevronRight className="h-4 w-4 shrink-0 text-default-secondary" />
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-elevation-subtle-soft p-4">
          <Button
            variant="ghost"
            className="gap-1"
            onClick={() => {
              if (isAdvancedStep) setStepIndex(REVIEW_INDEX);
              else if (stepIndex === 0) setStarted(false);
              else setStepIndex(stepIndex - 1);
            }}
          >
            <ChevronLeft className="h-4 w-4" /> {t_i18n("Back")}
          </Button>
          {step.key !== "review" && (
            <Button
              className="gap-1"
              // The walkthrough gates Next per step; in hub mode navigation is
              // free anyway (segments, hub rows), so Done never wedges the user
              // on a step — the Create gate re-checks everything instead.
              disabled={!canAdvance && !reviewReached}
              // After the walkthrough (and from any advanced option), a step
              // returns to the review hub instead of advancing linearly.
              onClick={() => setStepIndex(isAdvancedStep || reviewReached ? REVIEW_INDEX : stepIndex + 1)}
            >
              {isAdvancedStep || reviewReached ? t_i18n("Done") : t_i18n("Next")} <ChevronRight className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-3">
        {/* ── Canvas mirror — the visual construction support ── */}
        <div className="relative min-h-0 flex-1 overflow-hidden rounded-sm border border-elevation-subtle-soft">
          <WizardCanvas
            draft={{
              name,
              icon,
              persona,
              toolNames: toolIds.map((v) => resolveOptionLabel(toolOptions, v, unavailableLabel)),
              integrations: integrationIds.flatMap((id) => {
                const integ = connected.find((i) => i.id === id);
                return integ ? [{ id: integ.id, name: integ.name, provider: integ.provider }] : [];
              }),
              knowledgeBaseNames: kbIds.map((v) => resolveOptionLabel(kbOptions, v, unavailableLabel)),
              skillNames: skillIds.map((v) => resolveOptionLabel(skillOptions, v, unavailableLabel)),
              handoverNames: handoverIds.map((v) => resolveOptionLabel(agentOptions, v, unavailableLabel)),
              memoryTierLabels: [
                memory.short && t_i18n("Short-term Memory"),
                memory.medium && t_i18n("Medium-term Memory"),
                memory.persistent && t_i18n("Persistent Memory"),
                memory.user && t_i18n("User Memory"),
              ].filter((v): v is string => !!v),
              hasGuardrails: !!guardrails.trim(),
              modelLabel: chosenModel?.label || null,
              hasBudget: Object.values(budget).some((v) => v !== ""),
            }}
            highlight={step.highlight}
            assisting={playback.playing}
            onOpenStep={(key) => {
              const index = STEP_INDEX_BY_KEY[key as StepKey];
              if (index !== undefined) setStepIndex(index);
            }}
          />
          {/* Embedded tutorial — the step's documentation, over the canvas. */}
          {showDocs && (
            <div className="absolute inset-y-0 right-0 z-10 w-full max-w-md overflow-y-auto border-l border-elevation-subtle-soft layer-1 bg-elevation-default p-5 backdrop-blur-sm">
              <div className="mb-3 flex items-center justify-between">
                <p className="flex items-center gap-2 content-base font-semibold">
                  <GraduationCap className="h-4 w-4 text-accent" /> {t_i18n(step.title)}
                </p>
                <Button variant="ghost" size="sm" aria-label={t_i18n("Close")} onClick={() => setShowDocs(false)}>
                  <X className="h-4 w-4" />
                </Button>
              </div>
              <div className="space-y-3">
                {step.docs.map((paragraph, i) => (
                  <p key={i} className="content-compact leading-relaxed text-default-secondary">{t_i18n(paragraph)}</p>
                ))}
              </div>
            </div>
          )}
        </div>
        {/* Under the canvas, never over it: the canvas is what the assistant's
            changes are watched on. */}
        {assistantBar}
      </div>
    </div>
  );
}
