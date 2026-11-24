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

/**
 * Create or edit an agent account: its mode, the account and agent it
 * binds, its rules, how replies go out, when it is active, and a tester that
 * asks the rule engine what it would do with a message.
 */
import { useMemo, useState } from "react";
import { Bot, CalendarClock, FlaskConical, ListChecks, Plus, Send, Settings2, Trash2, UserRound } from "lucide-react";
import { ReactionFeedbackSettings } from "@/components/feedback/ReactionFeedbackSettings";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import {
  useAgentAccountCatalog,
  useAgentAccountIdentity,
  useCreateAgentAccount,
  useSimulateAgentAccount,
  useUpdateAgentAccount,
} from "@/hooks/api/useAgentAccountApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { DEFAULT_REACTION_FEEDBACK } from "@/lib/reaction-feedback";
import { cn } from "@/lib/utils";
import type {
  AgentAccount,
  AgentAccountActiveHours,
  AgentAccountCatalog,
  AgentAccountConversationType,
  AgentAccountMailboxActions as MailboxActions,
  AgentAccountMode,
  AgentAccountResponseMode,
  AgentAccountRule,
  AgentAccountSignatureSource,
  AgentAccountSimulateResponse,
  ReactionFeedbackSetting,
} from "@/types";
import { AgentAccountMailboxActions } from "./AgentAccountMailboxActions";
import { AgentAccountRules } from "./AgentAccountRules";
import { AgentAccountSignature } from "./AgentAccountSignature";
import {
  DAY_LABELS,
  MODE_DESCRIPTIONS,
  MODE_LABELS,
  RESPONSE_MODE_HINTS,
  RESPONSE_MODE_LABELS,
  SENDER_MATCH_LABELS,
  WEEKDAYS,
  accessSummary,
  decisionSummary,
  outcomeLabelKey,
  providerConversationTypes,
  rulesForProvider,
  senderMatch,
  wording,
} from "./agent-accounts";
import { Spinner, TimePicker } from "@filigran/design-system";

type Section = "general" | "rules" | "replies" | "schedule" | "test";

interface Draft {
  name: string;
  description: string;
  mode: AgentAccountMode;
  provider: string;
  integration_id: string;
  agent_id: string;
  enabled: boolean;
  response_mode: AgentAccountResponseMode;
  ai_label_enabled: boolean;
  ai_label_text: string;
  ai_label_position: "prefix" | "suffix";
  grace_period_seconds: number;
  owner_takeover_minutes: number;
  max_replies_per_hour: number;
  max_turns_per_hour: number;
  max_reply_recipients: number;
  context_messages: number;
  instructions: string;
  rules: AgentAccountRule[];
  active_hours: AgentAccountActiveHours | null;
  reaction_feedback: ReactionFeedbackSetting;
  notify_on_send: boolean;
  mailbox_actions: MailboxActions | null;
  signature_source: AgentAccountSignatureSource;
  signature_integration_id: string;
}

/** Nothing filed, until the catalog says what a mailbox files by default. */
const EMPTY_MAILBOX_ACTIONS: MailboxActions = {
  flag_needs_owner: false,
  notify_needs_owner: false,
  mark_read_replied: false,
  mark_read_no_reply: false,
  category_replied: "",
  category_needs_owner: "",
  category_no_reply: "",
};

/** What a new account of *mode* on *providerKey* starts from: the provider's preset. */
function presetDraft(catalog: AgentAccountCatalog | undefined, mode: AgentAccountMode, providerKey: string) {
  const preset = catalog?.providers.find((p) => p.key === providerKey)?.presets[mode] ?? catalog?.presets[mode];
  return {
    response_mode: preset?.response_mode ?? (mode === "ai_user" ? "auto" : "approval"),
    ai_label_enabled: preset?.ai_label_enabled ?? mode === "on_behalf",
    grace_period_seconds: preset?.grace_period_seconds ?? 0,
    owner_takeover_minutes: preset?.owner_takeover_minutes ?? 0,
    max_replies_per_hour: preset?.max_replies_per_hour ?? 20,
    max_turns_per_hour: preset?.max_turns_per_hour ?? 0,
    max_reply_recipients: preset?.max_reply_recipients ?? 10,
    context_messages: preset?.context_messages ?? 20,
    rules: preset?.rules ?? [],
  } satisfies Partial<Draft>;
}

const DEFAULT_SCHEDULE: AgentAccountActiveHours = {
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  mode: "inside",
  windows: [{ days: [1, 2, 3, 4, 5], start: "09:00", end: "18:00" }],
};

function draftOf(account: AgentAccount): Draft {
  return {
    name: account.name,
    description: account.description ?? "",
    mode: account.mode,
    provider: account.provider,
    integration_id: account.integration_id ?? "",
    agent_id: account.agent_id ?? "",
    enabled: account.enabled,
    response_mode: account.response_mode,
    ai_label_enabled: account.ai_label_enabled,
    ai_label_text: account.ai_label_text ?? "",
    ai_label_position: account.ai_label_position,
    grace_period_seconds: account.grace_period_seconds,
    owner_takeover_minutes: account.owner_takeover_minutes,
    max_replies_per_hour: account.max_replies_per_hour,
    max_turns_per_hour: account.max_turns_per_hour,
    max_reply_recipients: account.max_reply_recipients,
    context_messages: account.context_messages,
    instructions: account.instructions ?? "",
    rules: account.rules,
    active_hours: account.active_hours,
    reaction_feedback: account.reaction_feedback ?? DEFAULT_REACTION_FEEDBACK,
    notify_on_send: account.notify_on_send ?? true,
    mailbox_actions: account.mailbox_actions ?? null,
    signature_source: account.signature_source ?? "own",
    signature_integration_id: account.signature_integration_id ?? "",
  };
}

interface EditorProps {
  account: AgentAccount | null;
  onClose: () => void;
}

export function AgentAccountEditor({ account, onClose }: EditorProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: catalog } = useAgentAccountCatalog();
  const create = useCreateAgentAccount();
  const update = useUpdateAgentAccount();
  const [section, setSection] = useState<Section>("general");
  const [draft, setDraft] = useState<Draft | null>(account ? draftOf(account) : null);
  const readOnly = account ? !account.can_manage : false;

  const choose = (mode: AgentAccountMode) => {
    const provider = catalog?.providers[0]?.key ?? "microsoft_teams";
    setDraft({
      name: "",
      description: "",
      mode,
      provider,
      integration_id: "",
      agent_id: "",
      enabled: true,
      ...presetDraft(catalog, mode, provider),
      ai_label_text: "",
      ai_label_position: "suffix",
      instructions: "",
      active_hours: null,
      reaction_feedback: DEFAULT_REACTION_FEEDBACK,
      notify_on_send: true,
      mailbox_actions: catalog?.mailbox_defaults ?? null,
      signature_source: "own",
      signature_integration_id: "",
    });
  };

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => (d ? { ...d, [key]: value } : d));
  /** A new account on another platform starts again from that platform's preset: its rules speak of its conversations. */
  const setProvider = (provider: string) =>
    setDraft((d) => (d ? { ...d, provider, ...(account ? {} : presetDraft(catalog, d.mode, provider)) } : d));

  const providerInfo = catalog?.providers.find((p) => p.key === draft?.provider);
  const capabilities = providerInfo?.capabilities;
  const triages = !!capabilities?.triage;
  const signs = !!capabilities?.signature;
  const save = async () => {
    if (!draft) return;
    const settings = {
      ...(triages && draft.mailbox_actions ? { mailbox_actions: draft.mailbox_actions } : {}),
      ...(signs
        ? {
          signature_source: draft.signature_source,
          signature_integration_id: draft.signature_source === "integration" ? draft.signature_integration_id : "",
        }
        : {}),
      name: draft.name.trim(),
      description: draft.description,
      enabled: draft.enabled,
      response_mode: draft.response_mode,
      ai_label_enabled: draft.ai_label_enabled,
      ai_label_text: draft.ai_label_text,
      ai_label_position: draft.ai_label_position,
      grace_period_seconds: draft.grace_period_seconds,
      owner_takeover_minutes: draft.owner_takeover_minutes,
      max_replies_per_hour: draft.max_replies_per_hour,
      max_turns_per_hour: draft.max_turns_per_hour,
      max_reply_recipients: draft.max_reply_recipients,
      context_messages: draft.context_messages,
      instructions: draft.instructions,
      rules: rulesForProvider(draft.rules, providerInfo),
      reaction_feedback: draft.reaction_feedback,
      notify_on_send: draft.notify_on_send,
    };
    try {
      if (account) {
        await update.mutateAsync({
          id: account.id,
          ...settings,
          mode: draft.mode,
          integration_id: draft.integration_id || undefined,
          agent_id: draft.agent_id || undefined,
          ...(draft.active_hours ? { active_hours: draft.active_hours } : { clear_active_hours: true }),
        });
        toast({ title: t_i18n("Agent account saved"), variant: "success" });
      } else {
        await create.mutateAsync({
          ...settings,
          provider: draft.provider,
          mode: draft.mode,
          integration_id: draft.integration_id,
          agent_id: draft.agent_id,
          ...(draft.active_hours ? { active_hours: draft.active_hours } : {}),
        });
        toast({ title: t_i18n("Agent account created"), variant: "success" });
      }
      onClose();
    } catch (error) {
      toast({ title: t_i18n("Could not save the agent account"), description: (error as Error).message, variant: "error" });
    }
  };

  const { data: identity } = useAgentAccountIdentity(
    draft?.provider ?? "",
    draft?.integration_id || null,
    account?.id ?? null,
  );
  // One enabled agent account per platform account: the server refuses a second one too.
  const operatedElsewhere = !!identity && (identity.operated_by_account_id !== null || identity.operated_by_other_user);
  const saving = create.isPending || update.isPending;
  const canSave = !!draft && !readOnly && draft.name.trim() !== "" && !!draft.integration_id && !!draft.agent_id
    && !(draft.enabled && operatedElsewhere);

  const sections: { key: Section; label: string; icon: React.ElementType }[] = [
    { key: "general", label: t_i18n("General"), icon: Settings2 },
    { key: "rules", label: t_i18n("Rules"), icon: ListChecks },
    { key: "replies", label: t_i18n("Replies"), icon: Send },
    { key: "schedule", label: t_i18n("Schedule"), icon: CalendarClock },
    { key: "test", label: t_i18n("Test"), icon: FlaskConical },
  ];

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-h-[90vh] max-w-4xl">
        <DialogHeader>
          <DialogTitle>{account ? account.name : t_i18n("New agent account")}</DialogTitle>
          <DialogDescription>
            {t_i18n("An agent operating a real user account: yours, or a dedicated AI user. It hears what the account hears and answers under your rules.")}
          </DialogDescription>
        </DialogHeader>

        {!draft ? (
          <div className="grid gap-3 md:grid-cols-2">
            {(["on_behalf", "ai_user"] as AgentAccountMode[]).map((mode) => {
              const Icon = mode === "ai_user" ? Bot : UserRound;
              return (
                <button
                  key={mode}
                  type="button"
                  onClick={() => choose(mode)}
                  className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-4 text-left transition-colors hover:border-accent"
                >
                  <div className="mb-2 flex items-center gap-2">
                    <Icon className="h-5 w-5 text-accent" />
                    <span className="font-semibold">{t_i18n(MODE_LABELS[mode])}</span>
                  </div>
                  <p className="content-base text-default-secondary">{t_i18n(MODE_DESCRIPTIONS[mode])}</p>
                </button>
              );
            })}
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-1 border-b border-elevation-subtle pb-2">
              {sections.map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setSection(key)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-sm px-3 py-1.5 content-base transition-colors",
                    section === key ? "bg-accent/10 text-accent" : "text-default-secondary hover:text-default-primary",
                  )}
                >
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                  {key === "rules" && <CountBadge variant="secondary" className="ml-1">{draft.rules.length}</CountBadge>}
                </button>
              ))}
            </div>

            {/* The 320px floor stays inside the scroller: on the scroller, the dialog's flex
                column squeezes it under its content and draws the footer over it. The side
                padding keeps focus rings out of the scroller's clip. */}
            <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1">
              <div className="min-h-[320px] py-2">
                {section === "general" && (
                  <GeneralSection
                    draft={draft}
                    set={set}
                    setProvider={setProvider}
                    accountId={account?.id ?? null}
                    readOnly={readOnly}
                  />
                )}
                {section === "rules" && (
                  <AgentAccountRules
                    rules={draft.rules}
                    onChange={(rules) => set("rules", rules)}
                    provider={draft.provider}
                    integrationId={draft.integration_id || null}
                    agentId={draft.agent_id || null}
                    readOnly={readOnly}
                  />
                )}
                {section === "replies" && <RepliesSection draft={draft} set={set} account={account} readOnly={readOnly} />}
                {section === "schedule" && <ScheduleSection draft={draft} set={set} readOnly={readOnly} />}
                {section === "test" && <TestSection draft={draft} />}
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-elevation-subtle pt-3">
              <Button variant="ghost" onClick={onClose}>{t_i18n("Cancel")}</Button>
              {!readOnly && (
                <Button onClick={save} disabled={!canSave || saving} aria-busy={saving || undefined}>
                  {saving && <Spinner size="sm" tone="inherit" />}
                  {account ? t_i18n("Save") : t_i18n("Create")}
                </Button>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

type Setter = <K extends keyof Draft>(key: K, value: Draft[K]) => void;

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="content-base font-medium">{label}</div>
      {children}
      {hint && <p className="content-compact text-default-secondary">{hint}</p>}
    </div>
  );
}

function NumberInput({ value, onChange, min, max, disabled, label }: {
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  disabled?: boolean;
  /** The field's name: the label of the row it sits in. */
  label: string;
}) {
  return (
    <Input
      aria-label={label}
      type="number"
      min={min}
      max={max}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(Math.max(min, Math.min(max, Number(e.target.value) || 0)))}
    />
  );
}

/** Who the chosen integration is signed in as, and whether another agent account already operates it. */
function SignedInAs({ provider, integrationId, accountId }: {
  provider: string;
  integrationId: string;
  accountId: string | null;
}) {
  const { t_i18n } = useFormatter();
  const { data: identity, isLoading, isError } = useAgentAccountIdentity(provider, integrationId || null, accountId);
  if (!integrationId) return null;
  if (isLoading) return <Spinner size="sm" tone="inherit" className="text-default-secondary" />;
  if (isError || !identity) {
    return <p className="content-compact text-feedback-error-primary">{t_i18n("Could not read the account this integration is signed in as.")}</p>;
  }
  const account = identity.email ? `${identity.display_name} (${identity.email})` : identity.display_name;
  return (
    <div className="space-y-1 content-compact">
      <p className="flex items-center gap-1.5 text-default-secondary">
        <UserRound className="h-3.5 w-3.5 text-accent" />
        {t_i18n("Signed in as {account}", { account })}
      </p>
      {identity.operated_by_account_name && (
        <p className="text-feedback-warning-primary">
          {t_i18n("Your agent account \"{name}\" already operates this account: one agent account per platform account. Add rules to that one, or save this one switched off.", { name: identity.operated_by_account_name })}
        </p>
      )}
      {identity.operated_by_other_user && (
        <p className="text-feedback-warning-primary">
          {t_i18n("Another user's agent account already operates this account: one agent account per platform account.")}
        </p>
      )}
    </div>
  );
}

function GeneralSection({ draft, set, setProvider, accountId, readOnly }: {
  draft: Draft;
  set: Setter;
  setProvider: (provider: string) => void;
  accountId: string | null;
  readOnly: boolean;
}) {
  const isNew = accountId === null;
  const { t_i18n } = useFormatter();
  const { data: catalog } = useAgentAccountCatalog();
  const { data: integrations } = useIntegrations();
  const { data: agents } = useAgents();
  const provider = catalog?.providers.find((p) => p.key === draft.provider);
  const mailbox = provider?.kind === "mailbox";
  const candidates = useMemo(
    () => (integrations ?? []).filter((i) => !provider || i.provider === provider.integration_provider),
    [integrations, provider],
  );

  return (
    <fieldset disabled={readOnly} className="grid gap-4 md:grid-cols-2">
      <Row label={t_i18n("Name")}>
        <Input aria-label={t_i18n("Name")}
          value={draft.name}
          onChange={(e) => set("name", e.target.value)}
          placeholder={mailbox ? t_i18n("e.g. My mailbox delegate") : t_i18n("e.g. My Teams delegate")}
        />
      </Row>
      <Row label={t_i18n("Mode")} hint={t_i18n(MODE_DESCRIPTIONS[draft.mode])}>
        <Select value={draft.mode} onValueChange={(v) => set("mode", v as AgentAccountMode)}>
          <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="on_behalf">{t_i18n(MODE_LABELS.on_behalf)}</SelectItem>
            <SelectItem value="ai_user">{t_i18n(MODE_LABELS.ai_user)}</SelectItem>
          </SelectContent>
        </Select>
      </Row>
      <Row label={t_i18n("Platform")}>
        <SearchableSelect
          aria-label={t_i18n("Platform")}
          searchable="auto"
          options={(catalog?.providers ?? []).map((p) => ({ value: p.key, label: p.label }))}
          value={draft.provider}
          onValueChange={setProvider}
          disabled={!isNew}
        />
      </Row>
      <Row
        label={t_i18n("Account (integration)")}
        hint={t_i18n("An integration signed in as the account the agent operates: yours for a delegate, the service account's for an AI user.")}
      >
        <SearchableSelect
          aria-label={t_i18n("Account (integration)")}
          options={candidates.map((i) => ({ value: i.id, label: i.name }))}
          value={draft.integration_id}
          onValueChange={(v) => set("integration_id", v)}
          placeholder={t_i18n("Choose an integration")}
        />
        {candidates.length === 0 && (
          <p className="content-compact text-default-secondary">
            {t_i18n("Connect a {provider} integration signed in as the account first.", { provider: provider?.label ?? "" })}
          </p>
        )}
        <SignedInAs provider={draft.provider} integrationId={draft.integration_id} accountId={accountId} />
      </Row>
      <Row
        label={t_i18n("Agent")}
        hint={t_i18n("Its persona, knowledge and tools answer. Messages come from other people: prefer an agent whose tools you trust to run for them.")}
      >
        <SearchableSelect
          aria-label={t_i18n("Agent")}
          options={(agents ?? []).filter((a) => a.enabled !== false).map((a) => agentSelectOption(a))}
          value={draft.agent_id}
          onValueChange={(v) => set("agent_id", v)}
          placeholder={t_i18n("Choose an agent")}
        />
      </Row>
      <Row label={t_i18n("Enabled")}>
        <Switch aria-label={t_i18n("Enabled")} checked={draft.enabled} onCheckedChange={(c) => set("enabled", c)} />
      </Row>
      <div className="md:col-span-2">
        <Row label={t_i18n("Description")}>
          <Input aria-label={t_i18n("Description")} value={draft.description} onChange={(e) => set("description", e.target.value)} />
        </Row>
      </div>
      {provider && !provider.push_reachable && (
        <p className="rounded-sm border border-elevation-subtle bg-elevation-hover p-3 content-compact text-default-secondary md:col-span-2">
          {mailbox
            ? t_i18n("This platform has no public HTTPS address: the inbox is checked every minute instead of in real time.")
            : t_i18n("This platform has no public HTTPS address: direct and group chats are polled every minute, channels cannot be heard until BASE_URL is public.")}
        </p>
      )}
    </fieldset>
  );
}

function RepliesSection({ draft, set, account, readOnly }: {
  draft: Draft;
  set: Setter;
  account: AgentAccount | null;
  readOnly: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { data: catalog } = useAgentAccountCatalog();
  const provider = catalog?.providers.find((p) => p.key === draft.provider);
  const preview = draft.ai_label_text.trim()
    || (account?.ai_label_preview && !account.ai_label_text ? account.ai_label_preview : catalog?.default_labels[draft.mode] ?? "");

  return (
    <fieldset disabled={readOnly} className="space-y-5">
      <Row label={t_i18n("Response mode")} hint={t_i18n(RESPONSE_MODE_HINTS[draft.response_mode])}>
        <div className="flex flex-wrap gap-2">
          {(Object.keys(RESPONSE_MODE_LABELS) as AgentAccountResponseMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => set("response_mode", mode)}
              aria-pressed={draft.response_mode === mode}
              className={cn(
                "rounded-sm border px-3 py-1.5 content-base transition-colors",
                draft.response_mode === mode ? "border-accent bg-accent/10 text-accent" : "border-elevation-subtle text-default-secondary hover:border-accent/40",
              )}
            >
              {t_i18n(RESPONSE_MODE_LABELS[mode])}
            </button>
          ))}
        </div>
      </Row>

      <label className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle p-3">
        <span className="content-base font-medium">
          {t_i18n("Notify me when a reply is sent")}
          <span className="block content-compact font-normal text-default-secondary">
            {t_i18n("Each reply the account posts on its own reaches your notification bell and a live toast, with what was said and where - follow the account without opening the conversation.")}
          </span>
        </span>
        <Switch checked={draft.notify_on_send} onCheckedChange={(c) => set("notify_on_send", c)} />
      </label>

      <div className="space-y-3 rounded-sm border border-elevation-subtle p-3">
        <label className="flex items-center justify-between gap-3">
          <span className="content-base font-medium">
            {t_i18n("AI label")}
            <span className="block content-compact font-normal text-default-secondary">
              {t_i18n("Added in code to every reply, never left to the model. A rule can turn it on or off.")}
            </span>
          </span>
          <Switch checked={draft.ai_label_enabled} onCheckedChange={(c) => set("ai_label_enabled", c)} />
        </label>
        {draft.ai_label_enabled && (
          <div className="grid gap-3 md:grid-cols-[1fr_auto]">
            <Input aria-label={t_i18n("AI label")}
              value={draft.ai_label_text}
              onChange={(e) => set("ai_label_text", e.target.value)}
              maxLength={300}
              placeholder={catalog?.default_labels[draft.mode] ?? ""}
            />
            <Select value={draft.ai_label_position} onValueChange={(v) => set("ai_label_position", v as "prefix" | "suffix")}>
              <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="suffix">{t_i18n("After the reply")}</SelectItem>
                <SelectItem value="prefix">{t_i18n("Before the reply")}</SelectItem>
              </SelectContent>
            </Select>
            <p className="content-compact text-default-secondary md:col-span-2">
              {t_i18n("Use {name} for the account's name. Preview: {preview}", { name: "{name}", preview })}
            </p>
          </div>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Row label={t_i18n("Grace period (seconds)")} hint={t_i18n("Wait before answering; if the account's human answers meanwhile, the agent stays silent.")}>
          <NumberInput label={t_i18n("Grace period (seconds)")} value={draft.grace_period_seconds} onChange={(v) => set("grace_period_seconds", v)} min={0} max={3600} />
        </Row>
        <Row label={t_i18n("Step back after the owner writes (minutes)")} hint={t_i18n("Once the human writes in a conversation, the agent stays out of it for this long. 0: never.")}>
          <NumberInput label={t_i18n("Step back after the owner writes (minutes)")} value={draft.owner_takeover_minutes} onChange={(v) => set("owner_takeover_minutes", v)} min={0} max={1440} />
        </Row>
        <Row label={t_i18n("Replies per conversation and hour")} hint={t_i18n("Protects against loops, for instance between two AI accounts. 0: no limit.")}>
          <NumberInput label={t_i18n("Replies per conversation and hour")} value={draft.max_replies_per_hour} onChange={(v) => set("max_replies_per_hour", v)} min={0} max={1000} />
        </Row>
        <Row label={t_i18n("Agent turns per hour, whole account")} hint={t_i18n("Every message the agent takes up costs a turn, answered or not. Past the limit, the account leaves the next messages until the hour ends. 0: no limit.")}>
          <NumberInput label={t_i18n("Agent turns per hour, whole account")} value={draft.max_turns_per_hour} onChange={(v) => set("max_turns_per_hour", v)} min={0} max={1000} />
        </Row>
        {provider?.kind === "mailbox" && (
          <Row label={t_i18n("People a reply may reach")} hint={t_i18n("Besides the sender. A reply that would go to more people (a reply-all) is handed to you instead. 0: no limit.")}>
            <NumberInput label={t_i18n("People a reply may reach")} value={draft.max_reply_recipients} onChange={(v) => set("max_reply_recipients", v)} min={0} max={1000} />
          </Row>
        )}
        <Row label={t_i18n("Context messages")} hint={t_i18n("How many earlier messages of the thread the agent reads before answering.")}>
          <NumberInput label={t_i18n("Context messages")} value={draft.context_messages} onChange={(v) => set("context_messages", v)} min={0} max={50} />
        </Row>
      </div>

      {provider?.capabilities.signature && (
        <AgentAccountSignature
          source={draft.signature_source}
          integrationId={draft.signature_integration_id}
          providers={catalog?.signature_providers ?? []}
          onChange={(source, integrationId) => {
            set("signature_source", source);
            set("signature_integration_id", integrationId);
          }}
          disabled={readOnly}
        />
      )}

      {provider?.capabilities.triage && (
        <AgentAccountMailboxActions
          value={draft.mailbox_actions ?? catalog?.mailbox_defaults ?? account?.mailbox_actions ?? EMPTY_MAILBOX_ACTIONS}
          onChange={(value) => set("mailbox_actions", value)}
          disabled={readOnly}
        />
      )}

      {provider?.capabilities.reactions && (
        <ReactionFeedbackSettings
          value={draft.reaction_feedback}
          onChange={(value) => set("reaction_feedback", value)}
          available
          note={t_i18n("Reactions are read as they happen where the account hears every message; in channels heard for mentions only, and without a public HTTPS address, answers are checked every few minutes for 14 days.")}
          disabled={readOnly}
        />
      )}

      <Row label={t_i18n("Instructions")} hint={t_i18n("Added to the agent's persona for every reply of this account.")}>
        <div className="flex justify-end">
          <PromptGenerator
            contextType="agent_account_instructions"
            currentText={draft.instructions}
            onGenerated={(text) => set("instructions", text)}
            agentName={draft.name || undefined}
          />
        </div>
        <textarea
          value={draft.instructions}
          onChange={(e) => set("instructions", e.target.value)}
          rows={4}
          maxLength={4000}
          placeholder={t_i18n("e.g. Sign as Sam's assistant. Point people to the wiki for how-to questions.")}
          className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base text-default-primary placeholder:text-default-secondary focus:border-accent focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        />
      </Row>
    </fieldset>
  );
}

/** "HH:MM" (the stored window bound) as a Date of today for the design-system TimePicker; null when empty or malformed. */
function timeOfDay(value: string): Date | null {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const date = new Date();
  date.setHours(Number(match[1]), Number(match[2]), 0, 0);
  return date;
}

/** The TimePicker's Date back to the stored "HH:MM". */
function hhmm(date: Date): string {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function ScheduleSection({ draft, set, readOnly }: { draft: Draft; set: Setter; readOnly: boolean }) {
  const { t_i18n } = useFormatter();
  const schedule = draft.active_hours;
  const setSchedule = (next: AgentAccountActiveHours) => set("active_hours", next);
  const setWindow = (index: number, patch: Partial<AgentAccountActiveHours["windows"][number]>) =>
    schedule && setSchedule({ ...schedule, windows: schedule.windows.map((w, i) => (i === index ? { ...w, ...patch } : w)) });
  // A bound typed as text that does not resolve (25:99) keeps the stored time and shows its error inline (forms section 2).
  const [boundErrors, setBoundErrors] = useState<Record<string, boolean>>({});
  const onBound = (index: number, bound: "start" | "end") => (value: Date | null, context: { validationError: string | null }) => {
    const key = `${index}-${bound}`;
    const invalid = context.validationError !== null;
    setBoundErrors((prev) => (prev[key] === invalid ? prev : { ...prev, [key]: invalid }));
    if (value && !invalid) setWindow(index, { [bound]: hhmm(value) });
  };
  const boundError = (index: number, bound: "start" | "end") =>
    boundErrors[`${index}-${bound}`] ? t_i18n("Enter a time as HH:MM, from 00:00 to 23:59.") : undefined;

  return (
    <fieldset disabled={readOnly} className="space-y-4">
      <label className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle px-3 py-2">
        <span className="content-base font-medium">
          {t_i18n("Active hours")}
          <span className="block content-compact font-normal text-default-secondary">
            {t_i18n("Off: the account answers at any time.")}
          </span>
        </span>
        <Switch checked={!!schedule} onCheckedChange={(c) => set("active_hours", c ? DEFAULT_SCHEDULE : null)} />
      </label>
      {schedule && (
        <>
          <div className="grid gap-4 md:grid-cols-2">
            <Row label={t_i18n("Timezone")}>
              <Input aria-label={t_i18n("Timezone")} value={schedule.timezone} onChange={(e) => setSchedule({ ...schedule, timezone: e.target.value })} placeholder="Europe/Paris" />
            </Row>
            <Row label={t_i18n("Answer")}>
              <Select value={schedule.mode} onValueChange={(v) => setSchedule({ ...schedule, mode: v as "inside" | "outside" })}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="inside">{t_i18n("During these hours")}</SelectItem>
                  <SelectItem value="outside">{t_i18n("Outside these hours (when I am away)")}</SelectItem>
                </SelectContent>
              </Select>
            </Row>
          </div>
          <div className="space-y-2">
            {schedule.windows.map((window, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2 rounded-sm border border-elevation-subtle p-2">
                <div className="flex gap-1">
                  {WEEKDAYS.map((day) => {
                    const on = window.days.includes(day);
                    return (
                      <button
                        key={day}
                        type="button"
                        aria-pressed={on}
                        onClick={() =>
                          setWindow(index, {
                            days: on ? window.days.filter((d) => d !== day) : [...window.days, day].sort(),
                          })
                        }
                        className={cn(
                          "h-7 w-10 rounded-sm content-compact transition-colors",
                          on ? "bg-accent/15 text-accent" : "bg-elevation-hover text-default-secondary",
                        )}
                      >
                        {t_i18n(DAY_LABELS[day])}
                      </button>
                    );
                  })}
                </div>
                {/* The design-system TimePicker (#10533), 24-hour clock; a time that does not resolve leaves the bound as it was. */}
                <TimePicker
                  aria-label={t_i18n("From")}
                  format="HH:mm"
                  className="w-32"
                  disabled={readOnly}
                  value={timeOfDay(window.start)}
                  onChange={onBound(index, "start")}
                  error={boundError(index, "start")}
                  openClockLabel={t_i18n("Open clock")}
                  clockLabel={t_i18n("From clock")}
                  hoursLabel={t_i18n("Hours")}
                  minutesLabel={t_i18n("Minutes")}
                  confirmLabel={t_i18n("OK")}
                />
                <span className="content-compact text-default-secondary">-</span>
                <TimePicker
                  aria-label={t_i18n("To")}
                  format="HH:mm"
                  className="w-32"
                  disabled={readOnly}
                  value={timeOfDay(window.end)}
                  onChange={onBound(index, "end")}
                  error={boundError(index, "end")}
                  openClockLabel={t_i18n("Open clock")}
                  clockLabel={t_i18n("To clock")}
                  hoursLabel={t_i18n("Hours")}
                  minutesLabel={t_i18n("Minutes")}
                  confirmLabel={t_i18n("OK")}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={t_i18n("Remove")}
                  disabled={schedule.windows.length === 1}
                  onClick={() => setSchedule({ ...schedule, windows: schedule.windows.filter((_, i) => i !== index) })}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSchedule({ ...schedule, windows: [...schedule.windows, { days: [6, 7], start: "10:00", end: "12:00" }] })}
            >
              <Plus className="h-3.5 w-3.5" />
              {t_i18n("Add a window")}
            </Button>
          </div>
        </>
      )}
    </fieldset>
  );
}

function TestSection({ draft }: { draft: Draft }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const simulate = useSimulateAgentAccount();
  const { data: catalog } = useAgentAccountCatalog();
  const provider = catalog?.providers.find((p) => p.key === draft.provider);
  const words = wording(provider?.kind);
  const types = providerConversationTypes(provider);
  const [conversationType, setConversationType] = useState<AgentAccountConversationType>("group");
  const [mentioned, setMentioned] = useState(false);
  const [threadActive, setThreadActive] = useState(false);
  const [ownerActive, setOwnerActive] = useState(false);
  const [sender, setSender] = useState("");
  const [conversationId, setConversationId] = useState("");
  const [text, setText] = useState("Can you have a look at this?");
  const [result, setResult] = useState<AgentAccountSimulateResponse | null>(null);

  const conversations = draft.rules.flatMap((r) => r.conversations);
  const run = async () => {
    try {
      const response = await simulate.mutateAsync({
        mode: draft.mode,
        provider: draft.provider,
        settings: {
          enabled: draft.enabled,
          response_mode: draft.response_mode,
          ai_label_enabled: draft.ai_label_enabled,
          grace_period_seconds: draft.grace_period_seconds,
          owner_takeover_minutes: draft.owner_takeover_minutes,
          max_replies_per_hour: draft.max_replies_per_hour,
          instructions: draft.instructions,
          rules: rulesForProvider(draft.rules, provider),
          active_hours: draft.active_hours,
        },
        event: {
          conversation_type: conversationType,
          conversation_id: conversationId,
          sender_id: sender.includes("@") ? "" : sender,
          sender_email: sender.includes("@") ? sender : null,
          text,
          mentions_account: mentioned,
        },
        state: { thread_active: threadActive, owner_active: ownerActive, replies_last_hour: 0 },
      });
      setResult(response);
    } catch (error) {
      setResult(null);
      toast({ title: t_i18n("The rules cannot be tested"), description: (error as Error).message, variant: "error" });
    }
  };
  const toggles: { label: string; value: boolean; onChange: (value: boolean) => void }[] = [
    { label: t_i18n(words.testMention), value: mentioned, onChange: setMentioned },
    { label: t_i18n("The agent already answered in this thread"), value: threadActive, onChange: setThreadActive },
    { label: t_i18n("The owner just wrote here"), value: ownerActive, onChange: setOwnerActive },
  ];

  const summary = result ? decisionSummary(result.decision) : null;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div className="space-y-3">
        <p className="content-compact text-default-secondary">
          {t_i18n("Ask the rule engine what it would do with a message, with the settings on screen. Nothing is sent.")}
        </p>
        <Row label={t_i18n("Conversation")}>
          <Select value={conversationType} onValueChange={(v) => setConversationType(v as AgentAccountConversationType)}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              {types.map((kind) => (
                <SelectItem key={kind} value={kind}>{t_i18n(words.types[kind])}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Row>
        {conversations.length > 0 && (
          <Row label={t_i18n("Specific conversation")}>
            <SearchableSelect
              aria-label={t_i18n("Specific conversation")}
              options={conversations.map((c) => ({ value: c.id, label: c.name || c.id, keywords: [c.id] }))}
              pinned={[{ value: "__any__", label: t_i18n("Any other conversation") }]}
              value={conversationId || "__any__"}
              onValueChange={(v) => setConversationId(v === "__any__" ? "" : v)}
            />
          </Row>
        )}
        <Row
          label={t_i18n("Sender (id or email)")}
          hint={t_i18n("An email gives the groups of the XTM One user it belongs to.")}
        >
          <Input aria-label={t_i18n("Sender (id or email)")} value={sender} onChange={(e) => setSender(e.target.value)} placeholder="alice@example.com" />
        </Row>
        <Row label={t_i18n("Message")}>
          <Input aria-label={t_i18n("Message")} value={text} onChange={(e) => setText(e.target.value)} />
        </Row>
        <div className="space-y-2">
          {toggles.map(({ label, value, onChange }) => (
            <label key={label} className="flex items-center justify-between gap-3 content-base">
              {label}
              <Switch checked={value} onCheckedChange={onChange} />
            </label>
          ))}
        </div>
        <Button onClick={run} disabled={simulate.isPending} aria-busy={simulate.isPending || undefined}>
          {simulate.isPending && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Test")}
        </Button>
      </div>
      <div className="rounded-sm border border-elevation-subtle bg-elevation-hover p-4">
        {!result || !summary ? (
          <p className="content-base text-default-secondary">{t_i18n("The decision appears here.")}</p>
        ) : (
          <div className="space-y-3 content-base">
            <div className="flex items-center gap-2">
              <Badge variant={summary.answers ? "success" : "secondary"}>
                {summary.answers ? t_i18n("Answers") : t_i18n("Leaves it")}
              </Badge>
              <span>
                {summary.key === "unknown"
                  ? result.decision.reason
                  : t_i18n(outcomeLabelKey(summary.key, provider?.kind) ?? summary.key)}
              </span>
            </div>
            {result.decision.rule_id && (
              <p className="text-default-secondary">
                {t_i18n("Rule: {name}", { name: result.decision.rule_name || result.decision.rule_id })}
              </p>
            )}
            {(() => {
              const found = senderMatch(result.sender);
              return found && (
                <p className="text-default-secondary">{t_i18n(SENDER_MATCH_LABELS[found.key], found.values)}</p>
              );
            })()}
            {summary.answers && (
              <ul className="space-y-1 text-default-secondary">
                <li>
                  {provider?.capabilities.triage
                    ? result.decision.must_reply
                      ? t_i18n("The agent replies, or hands it to you when it cannot answer.")
                      : t_i18n("The agent replies, hands it to you, or leaves it when it needs no reply.")
                    : result.decision.must_reply
                      ? t_i18n("The agent must reply.")
                      : t_i18n("The agent decides whether to reply.")}
                </li>
                <li>{t_i18n(RESPONSE_MODE_LABELS[result.decision.response_mode])}</li>
                <li>{result.decision.ai_label ? t_i18n("With the AI label") : t_i18n("Without a label")}</li>
                {result.decision.grace_period_seconds > 0 && (
                  <li>{t_i18n("After a {seconds}s grace period", { seconds: result.decision.grace_period_seconds })}</li>
                )}
                <li>
                  {(() => {
                    const limited = accessSummary(result.decision.access ?? null);
                    if (!limited) return t_i18n("With everything the agent has");
                    return limited.memoryOff
                      ? t_i18n("With limited access ({count} lists narrowed), without memory", { count: limited.narrowed })
                      : t_i18n("With limited access ({count} lists narrowed)", { count: limited.narrowed });
                  })()}
                </li>
              </ul>
            )}
            <div className="border-t border-elevation-subtle pt-2 content-compact text-default-secondary">
              <div className="font-medium text-default-secondary">{t_i18n("What the account listens to")}</div>
              {provider?.kind === "mailbox" ? (
                <div>{result.listening.chats ? t_i18n("Its inbox") : t_i18n("Nothing: no rule answers an email")}</div>
              ) : (
                <>
                  <div>{result.listening.chats ? t_i18n("All its chats") : t_i18n("No chat")}</div>
                  <div>
                    {result.listening.all_channels
                      ? t_i18n("Every channel it belongs to")
                      : t_i18n("{count} chosen channels", { count: result.listening.channels.length })}
                  </div>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
