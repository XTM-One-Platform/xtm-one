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
 * The rules of an agent account: an ordered list, the first rule whose
 * scope and trigger match a message decides what happens to it.
 */
import { useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  AtSign,
  ChevronDown,
  ChevronRight,
  Crown,
  Hash,
  Megaphone,
  MessageSquare,
  Plus,
  ShieldCheck,
  Trash2,
  UsersRound,
  Users,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useAgent } from "@/hooks/api/useAgentApi";
import {
  useAgentAccountConversations,
  useAgentAccountPeople,
  useAgentAccountProvider,
} from "@/hooks/api/useAgentAccountApi";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useGroups } from "@/hooks/api/useGroupApi";
import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useSkills } from "@/hooks/api/useSkillApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type {
  AgentAccountConversationType,
  AgentAccountResponseMode,
  AgentAccountRule,
  AgentAccountRuleAccess,
  AgentAccountTarget,
} from "@/types";
import {
  ACCESS_LIST_LABELS,
  CONVERSATION_TYPES,
  RESPONSE_MODE_LABELS,
  type AccessListKey,
  accessAllows,
  accessSummary,
  agentCapabilities,
  moveItem,
  newRule,
  providerConversationTypes,
  ruleScope,
  ruleTemplate,
  toggleAccess,
  withMemory,
  wording,
} from "./agent-accounts";
import { Spinner } from "@filigran/design-system";

type Wording = ReturnType<typeof wording>;

const TYPE_ICONS = { direct: MessageSquare, group: Users, channel: Hash } as const;
const DEFAULT = "__default__";

interface RulesProps {
  rules: AgentAccountRule[];
  onChange: (rules: AgentAccountRule[]) => void;
  provider: string;
  integrationId: string | null;
  /** The account's agent: a rule's access lists its capabilities. */
  agentId: string | null;
  readOnly?: boolean;
}

export function AgentAccountRules({ rules, onChange, provider, integrationId, agentId, readOnly }: RulesProps) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState<string | null>(rules[0]?.id ?? null);
  const info = useAgentAccountProvider(provider);
  const words = wording(info?.kind);
  const types = providerConversationTypes(info);
  const channels = types.includes("channel");

  const update = (index: number, rule: AgentAccountRule) => onChange(rules.map((r, i) => (i === index ? rule : r)));
  const add = (rule: AgentAccountRule, first = false) => {
    onChange(first ? [rule, ...rules] : [...rules, rule]);
    setOpen(rule.id);
  };

  return (
    <div className="space-y-3">
      <p className="content-compact text-default-secondary">
        {t_i18n("Rules are read top to bottom: the first one that matches a message decides. A message no rule matches is left alone.")}
      </p>
      <p className="content-compact text-default-secondary">
        {channels
          ? t_i18n("Each rule can also limit what the agent uses for the people and conversations it covers. Put channel rules first: someone asking in a public channel then gets that channel's access, whoever they are.")
          : t_i18n("Each rule can also limit what the agent uses for the people it covers - nothing confidential for senders outside the company, for instance. Name a whole domain as @example.com.")}
      </p>
      {rules.length === 0 && (
        <div className="rounded-sm border border-dashed border-elevation-subtle p-4 text-center content-base text-default-secondary">
          {t_i18n("No rule yet: the account answers nothing.")}
        </div>
      )}
      {rules.map((rule, index) => (
        <RuleCard
          key={rule.id}
          rule={rule}
          index={index}
          total={rules.length}
          expanded={open === rule.id}
          onToggle={() => setOpen(open === rule.id ? null : rule.id)}
          onChange={(next) => update(index, next)}
          onMove={(delta) => onChange(moveItem(rules, index, delta))}
          onRemove={() => onChange(rules.filter((_, i) => i !== index))}
          provider={provider}
          integrationId={integrationId}
          agentId={agentId}
          readOnly={readOnly}
          types={types}
          words={words}
          pickConversations={info?.capabilities.conversations !== false}
        />
      ))}
      {!readOnly && (
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" onClick={() => add(newRule({ conversation_types: types }))}>
            <Plus className="h-3.5 w-3.5" />
            {t_i18n("Add a rule")}
          </Button>
          {channels && (
            <Button variant="ghost" size="sm" onClick={() => add(ruleTemplate("monitor_channel"))}>
              <Hash className="h-3.5 w-3.5" />
              {t_i18n("Monitor a channel (AI decides)")}
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => add({ ...ruleTemplate("vip"), conversation_types: types })}>
            <Users className="h-3.5 w-3.5" />
            {t_i18n("Focus on some people")}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => add({ ...ruleTemplate("mute"), conversation_types: types })}>
            <X className="h-3.5 w-3.5" />
            {channels ? t_i18n("Ignore a conversation") : t_i18n("Ignore some senders")}
          </Button>
          {channels && (
            <Button variant="ghost" size="sm" onClick={() => add(ruleTemplate("public_channel"), true)}>
              <Megaphone className="h-3.5 w-3.5" />
              {t_i18n("Public channels, non-confidential only")}
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => add(ruleTemplate("leadership"))}>
            <Crown className="h-3.5 w-3.5" />
            {t_i18n("Leadership groups, full access")}
          </Button>
        </div>
      )}
    </div>
  );
}

interface RuleCardProps {
  rule: AgentAccountRule;
  index: number;
  total: number;
  expanded: boolean;
  onToggle: () => void;
  onChange: (rule: AgentAccountRule) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
  provider: string;
  integrationId: string | null;
  agentId: string | null;
  readOnly?: boolean;
  /** The kinds of conversation the provider has, and how it names them. */
  types: AgentAccountConversationType[];
  words: Wording;
  /** Whether a rule can name specific conversations on this provider. */
  pickConversations: boolean;
}

function RuleCard({
  rule, index, total, expanded, onToggle, onChange, onMove, onRemove, provider, integrationId, agentId, readOnly,
  types, words, pickConversations,
}: RuleCardProps) {
  const { t_i18n } = useFormatter();
  const scope = ruleScope(rule, types);
  const limited = accessSummary(rule.access ?? null);
  const set = <K extends keyof AgentAccountRule>(key: K, value: AgentAccountRule[K]) => onChange({ ...rule, [key]: value });
  const toggleType = (kind: AgentAccountConversationType) => {
    const has = rule.conversation_types.includes(kind);
    const next = has ? rule.conversation_types.filter((t) => t !== kind) : [...rule.conversation_types, kind];
    set("conversation_types", CONVERSATION_TYPES.filter((t) => next.includes(t)));
  };
  const ignores = rule.action === "ignore";

  return (
    <div className={cn("rounded-sm border layer-1 bg-elevation-default", rule.enabled ? "border-elevation-subtle" : "border-elevation-subtle-soft opacity-70")}>
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
          <span className="content-compact text-default-secondary">{index + 1}.</span>
          <span className="truncate content-base font-medium">
            {rule.name || t_i18n("Untitled rule")}
          </span>
          <Badge variant={ignores ? "secondary" : "cyan"} className="content-caption">
            {ignores ? t_i18n("Ignore") : rule.reply === "decide" ? t_i18n("AI decides") : t_i18n("Always reply")}
          </Badge>
          {!ignores && limited && (
            <Badge variant="warning" className="gap-1 content-caption">
              <ShieldCheck className="h-3 w-3" />
              {t_i18n("Limited access")}
            </Badge>
          )}
          <span className="hidden truncate content-compact text-default-secondary md:inline">
            {scope.types.map((t) => t_i18n(words.types[t])).join(", ")}
            {" - "}
            {t_i18n(words.short[rule.trigger])}
            {scope.people > 0 && ` - ${t_i18n("{count} people", { count: scope.people })}`}
            {scope.groups > 0 && ` - ${t_i18n("{count} groups", { count: scope.groups })}`}
            {scope.conversations > 0 && ` - ${t_i18n("{count} conversations", { count: scope.conversations })}`}
          </span>
        </button>
        <Switch
          checked={rule.enabled}
          onCheckedChange={(checked) => set("enabled", checked)}
          disabled={readOnly}
          aria-label={t_i18n("Rule enabled")}
        />
        {!readOnly && (
          <>
            <Button variant="ghost" size="sm" onClick={() => onMove(-1)} disabled={index === 0} aria-label={t_i18n("Move up")}>
              <ArrowUp className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" onClick={() => onMove(1)} disabled={index === total - 1} aria-label={t_i18n("Move down")}>
              <ArrowDown className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" onClick={onRemove} aria-label={t_i18n("Delete rule")}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </>
        )}
      </div>
      {expanded && (
        <fieldset disabled={readOnly} className="space-y-4 border-t border-elevation-subtle px-3 py-3">
          <div className="grid gap-3 md:grid-cols-2">
            <Field label={t_i18n("Name")}>
              <Input aria-label={t_i18n("Name")} value={rule.name} onChange={(e) => set("name", e.target.value)} placeholder={t_i18n("e.g. My manager")} />
            </Field>
            <Field label={t_i18n("Action")}>
              <Select value={rule.action} onValueChange={(v) => set("action", v as AgentAccountRule["action"])}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="respond">{t_i18n("Answer")}</SelectItem>
                  <SelectItem value="ignore">{t_i18n("Ignore (stop here)")}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </div>

          <Field label={t_i18n("Where")}>
            <div className="flex flex-wrap gap-2">
              {types.map((kind) => {
                const Icon = TYPE_ICONS[kind];
                const on = rule.conversation_types.includes(kind);
                return (
                  <button
                    key={kind}
                    type="button"
                    onClick={() => toggleType(kind)}
                    aria-pressed={on}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-sm border px-2 py-1 content-compact transition-colors",
                      on ? "border-accent bg-accent/10 text-accent" : "border-elevation-subtle text-default-secondary hover:border-accent/40",
                    )}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {t_i18n(words.types[kind])}
                  </button>
                );
              })}
            </div>
          </Field>

          {pickConversations && (
            <ConversationPicker
              value={rule.conversations}
              onChange={(next) => set("conversations", next)}
              provider={provider}
              integrationId={integrationId}
              types={rule.conversation_types}
            />
          )}

          <div className="grid gap-3 md:grid-cols-3">
            <PeoplePicker
              label={t_i18n("Only these people")}
              hint={t_i18n("Empty, with no group either: anyone.")}
              value={rule.senders}
              onChange={(next) => set("senders", next)}
              provider={provider}
              integrationId={integrationId}
            />
            <GroupPicker value={rule.groups ?? []} onChange={(next) => set("groups", next)} />
            <PeoplePicker
              label={t_i18n("Except these people")}
              hint={t_i18n("Never answered by this rule.")}
              value={rule.exclude_senders}
              onChange={(next) => set("exclude_senders", next)}
              provider={provider}
              integrationId={integrationId}
            />
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <Field label={t_i18n("Trigger")} hint={t_i18n(words.hint)}>
              <Select value={rule.trigger} onValueChange={(v) => set("trigger", v as AgentAccountRule["trigger"])}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="mention">{t_i18n(words.trigger.mention)}</SelectItem>
                  <SelectItem value="any">{t_i18n(words.trigger.any)}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            {!ignores && (
              <Field label={t_i18n("Reply")} hint={t_i18n("AI decides: the agent reads every message and only speaks when it should, from its persona.")}>
                <Select value={rule.reply} onValueChange={(v) => set("reply", v as AgentAccountRule["reply"])}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="always">{t_i18n("Always reply")}</SelectItem>
                    <SelectItem value="decide">{t_i18n("AI decides whether to reply")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            )}
          </div>

          {!ignores && (
            <>
              <label className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle px-3 py-2">
                <span className="content-base">
                  {t_i18n("Follow the thread")}
                  <span className="block content-compact text-default-secondary">
                    {types.includes("channel")
                      ? t_i18n("Once the agent answered in a thread, later messages there count as addressed and the agent decides. In a channel heard for mentions only, the thread is checked every minute for a day.")
                      : t_i18n("Once the agent answered in an email thread, later emails there count as addressed, even with the account only in Cc, and the agent decides.")}
                  </span>
                </span>
                <Switch checked={rule.continue_threads} onCheckedChange={(c) => set("continue_threads", c)} />
              </label>

              <div className="grid gap-3 md:grid-cols-3">
                <Field label={t_i18n("Response mode")}>
                  <Select
                    value={rule.response_mode ?? DEFAULT}
                    onValueChange={(v) => set("response_mode", v === DEFAULT ? null : (v as AgentAccountResponseMode))}
                  >
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value={DEFAULT}>{t_i18n("Account default")}</SelectItem>
                      {(Object.keys(RESPONSE_MODE_LABELS) as AgentAccountResponseMode[]).map((mode) => (
                        <SelectItem key={mode} value={mode}>{t_i18n(RESPONSE_MODE_LABELS[mode])}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <Field label={t_i18n("AI label")}>
                  <Select
                    value={rule.ai_label === null ? DEFAULT : rule.ai_label ? "on" : "off"}
                    onValueChange={(v) => set("ai_label", v === DEFAULT ? null : v === "on")}
                  >
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value={DEFAULT}>{t_i18n("Account default")}</SelectItem>
                      <SelectItem value="on">{t_i18n("Add the label")}</SelectItem>
                      <SelectItem value="off">{t_i18n("No label")}</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
                <Field label={t_i18n("Grace period (seconds)")}>
                  <Input aria-label={t_i18n("Grace period (seconds)")}
                    type="number"
                    min={0}
                    max={3600}
                    value={rule.grace_period_seconds ?? ""}
                    placeholder={t_i18n("Account default")}
                    onChange={(e) =>
                      set("grace_period_seconds", e.target.value === "" ? null : Math.max(0, Math.min(3600, Number(e.target.value))))
                    }
                  />
                </Field>
              </div>

              <AccessEditor
                access={rule.access ?? null}
                onChange={(next) => set("access", next)}
                agentId={agentId}
              />

              <Field
                label={t_i18n("Guardrails and instructions for this rule")}
                hint={t_i18n("Added to the agent's persona for the messages this rule answers. The access above is enforced by the platform; these words govern what the agent says.")}
              >
                <div className="flex justify-end">
                  <PromptGenerator
                    contextType="agent_account_rule_instructions"
                    currentText={rule.instructions}
                    onGenerated={(text) => set("instructions", text)}
                  />
                </div>
                <textarea
                  value={rule.instructions}
                  onChange={(e) => set("instructions", e.target.value)}
                  rows={3}
                  maxLength={4000}
                  placeholder={t_i18n("e.g. Be formal and never commit to a date.")}
                  className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base text-default-primary placeholder:text-default-secondary focus:border-accent focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                />
              </Field>
            </>
          )}
        </fieldset>
      )}
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="content-compact font-medium text-default-secondary">{label}</div>
      {children}
      {hint && <p className="text-[11px] text-default-secondary">{hint}</p>}
    </div>
  );
}

function Chips({ items, onRemove, icon: Icon }: {
  items: AgentAccountTarget[];
  onRemove: (id: string) => void;
  icon: React.ElementType;
}) {
  const { t_i18n } = useFormatter();
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <span key={item.id} className="inline-flex max-w-full items-center gap-1 rounded-sm border border-elevation-subtle bg-elevation-hover px-2 py-0.5 content-compact">
          <Icon className="h-3 w-3 shrink-0 text-default-secondary" />
          <span className="truncate">{item.name || item.email || item.id}</span>
          <button type="button" onClick={() => onRemove(item.id)} aria-label={t_i18n("Remove")} className="text-default-secondary hover:text-default-primary">
            <X className="h-3 w-3" />
          </button>
        </span>
      ))}
    </div>
  );
}

function ConversationPicker({ value, onChange, provider, integrationId, types }: {
  value: AgentAccountTarget[];
  onChange: (value: AgentAccountTarget[]) => void;
  provider: string;
  integrationId: string | null;
  types: AgentAccountConversationType[];
}) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const { data, isFetching, error } = useAgentAccountConversations(provider, browsing ? integrationId : null, query);
  const chosen = useMemo(() => new Set(value.map((v) => v.id)), [value]);
  const options = (data ?? []).filter((o) => (types.length ? types.includes(o.type) : true) && !chosen.has(o.id));

  return (
    <div className="space-y-1.5">
      <div className="content-compact font-medium text-default-secondary">{t_i18n("Specific conversations")}</div>
      <Chips items={value} icon={Hash} onRemove={(id) => onChange(value.filter((v) => v.id !== id))} />
      {!browsing ? (
        <Button variant="ghost" size="sm" onClick={() => setBrowsing(true)} disabled={!integrationId}>
          <Plus className="h-3.5 w-3.5" />
          {t_i18n("Choose conversations")}
        </Button>
      ) : (
        <div className="space-y-2 rounded-sm border border-elevation-subtle p-2">
          <Input aria-label={t_i18n("Search chats and channels")} value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t_i18n("Search chats and channels")} />
          <div className="max-h-48 space-y-0.5 overflow-y-auto">
            {isFetching && !data && <p className="px-2 py-1 content-compact text-default-secondary">{t_i18n("Loading...")}</p>}
            {error && <p className="px-2 py-1 content-compact text-feedback-error-primary">{(error as Error).message}</p>}
            {data && options.length === 0 && (
              <p className="px-2 py-1 content-compact text-default-secondary">{t_i18n("No conversation matches.")}</p>
            )}
            {options.map((option) => {
              const Icon = TYPE_ICONS[option.type] ?? Hash;
              return (
                <button
                  key={option.id}
                  type="button"
                  onClick={() =>
                    onChange([...value, { id: option.id, name: option.name, type: option.type, team_id: option.team_id ?? undefined }])
                  }
                  className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left content-base hover:bg-elevation-hover"
                >
                  <Icon className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
                  <span className="truncate">{option.name}</span>
                </button>
              );
            })}
          </div>
          <Button variant="ghost" size="sm" onClick={() => setBrowsing(false)}>{t_i18n("Done")}</Button>
        </div>
      )}
      <p className="text-[11px] text-default-secondary">{t_i18n("Empty: every conversation of the types above.")}</p>
    </div>
  );
}

function GroupPicker({ value, onChange }: {
  value: AgentAccountTarget[];
  onChange: (value: AgentAccountTarget[]) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: groups } = useGroups();
  const chosen = new Set(value.map((v) => v.id));
  const options = (groups ?? []).filter((g) => !chosen.has(g.id));

  return (
    <div className="space-y-1.5">
      <div className="content-compact font-medium text-default-secondary">{t_i18n("Members of these groups")}</div>
      <Chips items={value} icon={UsersRound} onRemove={(id) => onChange(value.filter((v) => v.id !== id))} />
      {/* Keyed on the count: each addition starts an empty field again. */}
      <SearchableSelect
        key={value.length}
        aria-label={t_i18n("Add a group")}
        options={options.map((group) => ({ value: group.id, label: group.name }))}
        value=""
        onValueChange={(id) => {
          const group = (groups ?? []).find((g) => g.id === id);
          if (group) onChange([...value, { id: group.id, name: group.name }]);
        }}
        placeholder={t_i18n("Add a group")}
      />
      <p className="text-[11px] text-default-secondary">
        {t_i18n("XTM One groups: a sender belongs when their email is the one of an active member.")}
      </p>
    </div>
  );
}

/** What the agent may use for this rule's messages: its own capabilities, each switchable. */
function AccessEditor({ access, onChange, agentId }: {
  access: AgentAccountRuleAccess | null;
  onChange: (access: AgentAccountRuleAccess | null) => void;
  agentId: string | null;
}) {
  const { t_i18n } = useFormatter();
  const { data: agent, isLoading } = useAgent(agentId ?? "");
  const { data: knowledgeBases } = useBindableKnowledgeBases();
  const { data: mcpServers } = useMCPServers();
  const { data: customTools } = useCustomTools();
  const { data: skills } = useSkills();
  const capabilities = useMemo(
    () =>
      agent
        ? agentCapabilities(agent, {
            knowledge_base_ids: knowledgeBases,
            mcp_server_ids: mcpServers,
            custom_tool_ids: customTools,
            skill_ids: skills,
          })
        : [],
    [agent, knowledgeBases, mcpServers, customTools, skills],
  );

  return (
    <div className="space-y-3 rounded-sm border border-elevation-subtle p-3">
      <div>
        <div className="flex items-center gap-1.5 content-base font-medium">
          <ShieldCheck className="h-4 w-4 text-accent" />
          {t_i18n("Access for this rule")}
        </div>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("What the agent may use when it answers the messages of this rule, enforced by the platform. Everything is on by default; switch off what this audience must not reach.")}
        </p>
      </div>
      {!agentId ? (
        <p className="content-compact text-default-secondary">{t_i18n("Choose the account's agent first.")}</p>
      ) : isLoading ? (
        <Spinner size="sm" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
      ) : (
        <>
          {capabilities.length === 0 && (
            <p className="content-compact text-default-secondary">{t_i18n("The agent has no integration, knowledge base, MCP server, tool or skill to limit.")}</p>
          )}
          {capabilities.map(({ key, items }) => (
            <div key={key} className="space-y-1.5">
              <div className="content-compact font-medium text-default-secondary">{t_i18n(ACCESS_LIST_LABELS[key])}</div>
              <div className="flex flex-wrap gap-1.5">
                {items.map((item) => {
                  const on = accessAllows(access, key as AccessListKey, item.id);
                  return (
                    <button
                      key={item.id}
                      type="button"
                      aria-pressed={on}
                      onClick={() => onChange(toggleAccess(access, key, item.id, items.map((i) => i.id)))}
                      className={cn(
                        "max-w-full truncate rounded-sm border px-2 py-0.5 content-compact transition-colors",
                        on ? "border-accent bg-accent/10 text-accent" : "border-elevation-subtle text-default-secondary line-through hover:border-accent/40",
                      )}
                    >
                      {item.label}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
          <label className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle px-3 py-2">
            <span className="content-base">
              {t_i18n("Use the agent's memory")}
              <span className="block content-compact text-default-secondary">
                {t_i18n("Off: the agent neither reads nor writes its memory for these messages, so what it learned elsewhere - in a board chat, say - cannot surface here.")}
              </span>
            </span>
            <Switch checked={(access?.memory ?? "on") === "on"} onCheckedChange={(c) => onChange(withMemory(access, c))} />
          </label>
        </>
      )}
    </div>
  );
}

function PeoplePicker({ label, hint, value, onChange, provider, integrationId }: {
  label: string;
  hint: string;
  value: AgentAccountTarget[];
  onChange: (value: AgentAccountTarget[]) => void;
  provider: string;
  integrationId: string | null;
}) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const { data, isFetching } = useAgentAccountPeople(provider, integrationId, query);
  const chosen = new Set(value.map((v) => v.id));
  const typedEmail = query.includes("@") && !chosen.has(query.trim()) ? query.trim() : null;

  return (
    <div className="space-y-1.5">
      <div className="content-compact font-medium text-default-secondary">{label}</div>
      <Chips items={value} icon={AtSign} onRemove={(id) => onChange(value.filter((v) => v.id !== id))} />
      <Input aria-label={label}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={integrationId ? t_i18n("Search people, or type an email or @domain") : t_i18n("Type an email or @domain")}
      />
      {query.trim().length >= 2 && (
        <div className="max-h-40 space-y-0.5 overflow-y-auto rounded-sm border border-elevation-subtle p-1">
          {isFetching && !data && <p className="px-2 py-1 content-compact text-default-secondary">{t_i18n("Loading...")}</p>}
          {(data ?? []).filter((p) => !chosen.has(p.id)).map((person) => (
            <button
              key={person.id}
              type="button"
              onClick={() => {
                onChange([...value, { id: person.id, name: person.name, email: person.email ?? undefined }]);
                setQuery("");
              }}
              className="flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1 text-left content-base hover:bg-elevation-hover"
            >
              <span className="truncate">{person.name}</span>
              <span className="truncate content-compact text-default-secondary">{person.email}</span>
            </button>
          ))}
          {typedEmail && (
            <button
              type="button"
              onClick={() => {
                onChange([...value, { id: typedEmail, email: typedEmail, name: typedEmail }]);
                setQuery("");
              }}
              className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left content-base hover:bg-elevation-hover"
            >
              <Plus className="h-3.5 w-3.5" />
              {t_i18n("Add {email}", { email: typedEmail })}
            </button>
          )}
        </div>
      )}
      <p className="text-[11px] text-default-secondary">{hint}</p>
    </div>
  );
}
