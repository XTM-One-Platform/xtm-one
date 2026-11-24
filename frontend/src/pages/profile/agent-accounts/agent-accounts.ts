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
 * Agent accounts: the words and the pure helpers of the editor.
 *
 * The rule engine lives in the backend (`services/agent_accounts/rules.py`);
 * this module only names its codes and builds the shapes it stores.
 */
import type {
  Agent,
  AgentAccountActivity,
  AgentAccountActivityGroup,
  AgentAccountActivityPeriod,
  AgentAccountConversationType,
  AgentAccountDecision,
  AgentAccountMode,
  AgentAccountProviderInfo,
  AgentAccountProviderKind,
  AgentAccountReply,
  AgentAccountReplyFilters,
  AgentAccountResponseMode,
  AgentAccountRule,
  AgentAccountRuleAccess,
  AgentAccountSendOrigin,
  AgentAccountSenderMatch,
  AgentAccountSentKind,
  AgentAccountSignatureSource,
  AgentAccountSyncProblem,
  AgentAccountSyncProblemKind,
  AgentAccountSyncReport,
  AgentAccountSyncTarget,
} from "@/types";

/** Why the engine answered or left a message, read by `t_i18n(DECISION_REASON_LABELS[reason])`. @i18n-keys */
export const DECISION_REASON_LABELS = {
  respond: "The agent answers",
  account_disabled: "The account is paused",
  no_agent: "The account has no agent",
  own_message: "The account wrote it",
  application_author: "An app or a bot wrote it",
  agent_author: "Another agent account wrote it",
  empty_message: "The message has no text",
  outside_active_hours: "Outside the active hours",
  ignored_by_rule: "A rule ignores it",
  not_addressed: "The account is not mentioned",
  no_matching_rule: "No rule covers it",
  owner_takeover: "The account owner took over",
  rate_limited: "Hourly reply limit reached",
  account_rate_limited: "Hourly limit of the account reached",
} as const;

/** Who the sender was found to be, read by `t_i18n(SENDER_MATCH_LABELS[key], values)`. @i18n-keys */
export const SENDER_MATCH_LABELS = {
  member: "XTM One user {emails}, member of {groups}",
  outside: "XTM One user {emails}, in none of the groups the rules name",
  unknown: "No active XTM One user has the email {emails}",
  no_email: "The platform gave no email for the sender",
} as const;

/** What a decision says about its sender, or null when no rule named a group. */
export function senderMatch(sender: AgentAccountSenderMatch | null | undefined): {
  key: keyof typeof SENDER_MATCH_LABELS;
  values: { emails: string; groups: string };
} | null {
  if (!sender) return null;
  const values = { emails: sender.emails.join(", "), groups: sender.groups.map((g) => g.name).join(", ") };
  if (!sender.emails.length) return { key: "no_email", values };
  if (!sender.known) return { key: "unknown", values };
  return { key: sender.groups.length ? "member" : "outside", values };
}

/** The reasons a mailbox says in its own words, by the same codes. @i18n-keys */
export const MAILBOX_REASON_LABELS = {
  not_addressed: "The account is not in To",
  application_author: "Automated mail (a notification, a newsletter, an auto-reply)",
} as const;

/** Where a reply's signature comes from, in the order the editor offers them. */
export const SIGNATURE_SOURCES = ["own", "integration", "none"] as const satisfies readonly AgentAccountSignatureSource[];

/** @i18n-keys */
export const SIGNATURE_SOURCE_LABELS = {
  own: "The account's own",
  integration: "From a signature integration",
  none: "None",
} as const satisfies Record<AgentAccountSignatureSource, string>;

/** @i18n-keys */
export const SIGNATURE_SOURCE_HINTS = {
  own: "Copied from the account's sent mail: the signature its latest emails carried.",
  integration: "Read when each reply is sent: the person's current signature and banner, its reply variant first. When it cannot be read, the reply carries the account's own, and the account says why.",
  none: "The mail server signs every outgoing email in transit (a server-side signature): the account adds none, so replies are never signed twice.",
} as const satisfies Record<AgentAccountSignatureSource, string>;

/** What happened to a message after the decision. @i18n-keys */
export const OUTCOME_LABELS = {
  sent: "Sent",
  flagged: "Handed to you",
  pending: "Waiting for approval",
  shadow: "Recorded (shadow)",
  silent: "The agent stayed silent",
  failed: "Failed",
  waiting: "Grace period",
  owner_replied: "The owner answered first",
  agent_author: "Another agent account wrote it",
  ai_unavailable: "AI unavailable",
  agent_unavailable: "Agent unavailable",
  rejected: "Rejected",
  expired: "Expired",
  already_handled: "Handled elsewhere",
  // One turn per chat: a message that arrived while the agent answered,
  // and one answered together with a later message of the same conversation.
  queued: "Queued",
  coalesced: "Answered together",
} as const;

/** @i18n-keys */
export const MODE_LABELS = {
  on_behalf: "Personal delegate",
  ai_user: "AI user",
} as const satisfies Record<AgentAccountMode, string>;

/** @i18n-keys */
export const MODE_DESCRIPTIONS = {
  on_behalf:
    "Your own account. The agent answers on your behalf when you are mentioned or messaged, under your rules: approval first, an AI label, and it steps back when you answer.",
  ai_user:
    "A dedicated account everyone knows is an AI, with its own mailbox and calendar. It answers like a colleague, can follow whole channels and decides when to speak.",
} as const satisfies Record<AgentAccountMode, string>;

/** @i18n-keys */
export const RESPONSE_MODE_LABELS = {
  auto: "Send automatically",
  approval: "Ask me first",
  shadow: "Shadow (record only)",
} as const satisfies Record<AgentAccountResponseMode, string>;

/** @i18n-keys */
export const RESPONSE_MODE_HINTS = {
  auto: "Replies are posted as the account as soon as they are written.",
  approval: "Replies wait in your approval queue; you send, edit or drop each one.",
  shadow: "Nothing is posted: see what the agent would have said, to tune the rules safely.",
} as const satisfies Record<AgentAccountResponseMode, string>;

/** @i18n-keys */
const CONVERSATION_TYPE_LABELS = {
  direct: "Direct messages",
  group: "Group chats",
  channel: "Channels",
} as const satisfies Record<AgentAccountConversationType, string>;

/** The kind of one conversation, which also names a chat the platform gave no name. @i18n-keys */
const CONVERSATION_KIND_LABELS = {
  direct: "Direct message",
  group: "Group chat",
  channel: "Channel",
} as const satisfies Record<AgentAccountConversationType, string>;

/** How a mailbox names the kinds of conversation. @i18n-keys */
export const MAILBOX_CONVERSATION_TYPE_LABELS = {
  direct: "Emails to the account alone",
  group: "Emails with other recipients",
  channel: "Channels",
} as const satisfies Record<AgentAccountConversationType, string>;

/** @i18n-keys */
const MAILBOX_CONVERSATION_KIND_LABELS = {
  direct: "Email to the account alone",
  group: "Email with other recipients",
  channel: "Channel",
} as const satisfies Record<AgentAccountConversationType, string>;

/** A rule's trigger and how a message is addressed, in each kind of platform's words. @i18n-keys */
export const ADDRESSING_LABELS = {
  trigger_mention: "Only when the account is mentioned",
  trigger_any: "Every message (full monitoring)",
  trigger_mention_mailbox: "Only when the account is in To",
  trigger_any_mailbox: "Every email, Cc included",
  short_mention: "when mentioned",
  short_any: "every message",
  short_mention_mailbox: "when in To",
  short_any_mailbox: "every email",
  hint: "A direct message always counts as addressed to the account.",
  hint_mailbox: "An email to the account alone always counts as addressed to it; Cc alone does not.",
  test_mention: "The account is mentioned",
  test_mention_mailbox: "The account is in To",
  how_direct: "A direct message",
  how_direct_mailbox: "An email to the account alone",
  how_mention: "The account was mentioned",
  how_mention_mailbox: "The account was in To",
  how_none: "Not addressed to the account",
  how_none_mailbox: "The account was not in To",
} as const;

type AddressingKey = keyof typeof ADDRESSING_LABELS;

/** The words the editor and the account page use for a provider of *kind*. */
export function wording(kind: AgentAccountProviderKind | undefined) {
  const mailbox = kind === "mailbox";
  const pick = (key: string): string => ADDRESSING_LABELS[(mailbox ? `${key}_mailbox` : key) as AddressingKey];
  return {
    types: mailbox ? MAILBOX_CONVERSATION_TYPE_LABELS : CONVERSATION_TYPE_LABELS,
    kinds: mailbox ? MAILBOX_CONVERSATION_KIND_LABELS : CONVERSATION_KIND_LABELS,
    trigger: { mention: pick("trigger_mention"), any: pick("trigger_any") },
    short: { mention: pick("short_mention"), any: pick("short_any") },
    hint: pick("hint"),
    testMention: pick("test_mention"),
    how: { direct: pick("how_direct"), mention: pick("how_mention"), none: pick("how_none") },
  };
}

/** The kinds of conversation a provider has, in the engine's order. */
export function providerConversationTypes(
  provider: Pick<AgentAccountProviderInfo, "capabilities"> | undefined,
): AgentAccountConversationType[] {
  return CONVERSATION_TYPES.filter((type) => provider?.capabilities[type] !== false);
}

/**
 * The rules as the API takes them for *provider*, which refuses a conversation
 * type the provider does not have. The editor never offers one, but a
 * rule saved before that check may still name it next to the provider's own
 * types: it is dropped, the rule reaching what it reached. A rule naming none
 * of the provider's types is sent as it is, so the API says what is wrong
 * instead of the rule silently widening to every type.
 */
export function rulesForProvider(
  rules: AgentAccountRule[],
  provider: Pick<AgentAccountProviderInfo, "capabilities"> | undefined,
): AgentAccountRule[] {
  const types = providerConversationTypes(provider);
  return rules.map((rule) => {
    const kept = rule.conversation_types.filter((type) => types.includes(type));
    return kept.length === rule.conversation_types.length || kept.length === 0
      ? rule
      : { ...rule, conversation_types: kept };
  });
}

/** @i18n-keys */
export const STATUS_LABELS = {
  active: "Active",
  paused: "Paused",
  error: "Error",
  setup: "Setting up",
} as const;

/** @i18n-keys */
export const TRANSPORT_LABELS = {
  push: "Real time",
  poll: "Polling every minute",
  mixed: "Real time and polling",
  none: "Not listening",
} as const;

/**
 * What keeps the account from hearing a conversation, by copy key:
 * the problem's kind, or a variant of it when the problem names its holder or
 * permission (`syncProblemCopy`). Placeholders: `{provider}`, `{holder}`, `{permission}`.
 * @i18n-keys
 */
export const SYNC_PROBLEM_TITLES = {
  integration: "The account's integration cannot be used",
  reauthorize: "{provider} rejected the integration's sign-in",
  operated: "Another user's agent account already operates this account",
  operated_own: "Your agent account {holder} already operates this account",
  no_public_address: "Channels cannot be heard without a public HTTPS address",
  unreachable: "{provider} could not reach the platform to confirm the subscription",
  refused: "{provider} refused to notify the account",
  taken_by_account: "Another user's agent account already follows it",
  taken_by_own_account: "Your agent account {holder} already follows it",
  taken_elsewhere: "Another platform already follows it through the same app",
  taken_elsewhere_named: "{holder} already follows it through the same app",
  not_found: "The conversation no longer exists, or the account left it",
  not_subscribed: "The rules cover it, but the account does not listen to it",
  throttled: "{provider} asked the platform to slow down",
  unavailable: "{provider} did not answer",
  error: "{provider} answered with an error",
  reactions_refused: "{provider} refused to notify the reactions",
} as const;

/** What to do about each problem, by the same copy keys. @i18n-keys */
export const SYNC_PROBLEM_HINTS = {
  integration: "Open the integration and sign it in as the account, or pick another integration in the account's settings, then synchronize.",
  reauthorize: "Sign the integration in again as the account, then synchronize.",
  operated: "One agent account per platform account: switch this one off, or ask the owner of the other one to.",
  operated_own: "One agent account per platform account: add these rules to {holder}, or switch one of the two off.",
  no_public_address: "Direct and group chats are checked every minute meanwhile. An administrator sets BASE_URL to a public HTTPS address to hear channels.",
  unreachable: "The platform's notification address must be reachable from the internet over HTTPS, with no VPN or access proxy in front of it. An administrator checks BASE_URL, then synchronize.",
  refused: "The account may lack a permission or no longer be a member of the conversation. Check the integration's permissions, then synchronize.",
  refused_permission: "The integration needs the {permission} permission - an administrator may have to consent to it - and the account must be a member of the conversation. Once granted, sign the integration in again, then synchronize.",
  taken_by_account: "{provider} allows one subscription per app and conversation. Remove the conversation from one of the two accounts' rules, or connect one of them through another app.",
  taken_by_own_account: "{provider} allows one subscription per app and conversation: remove the conversation from the rules of one of the two accounts.",
  taken_elsewhere: "{provider} allows one subscription per app and conversation, and another platform signed in as this account holds it. Remove it there, or connect one of the two through another app.",
  not_found: "Remove it from the rules, or add the account back to the conversation.",
  not_subscribed: "A rule answering in every channel follows a limited number of channels, and none in a team whose channels {provider} does not list to the account. Name the channel in a rule, or pick it again in the rule if it has no team.",
  throttled: "Nothing to do: the next synchronization tries again.",
  unavailable: "Nothing to do: the next synchronization tries again. If it lasts, check that the platform can reach the provider.",
  error: "The technical details below say what the provider answered.",
  reactions_refused: "The conversation is still heard. Reactions on the account's answers there are read periodically instead, so ratings may arrive later. A synchronization asks again after a day.",
} as const;

/**
 * The reasons the platform words itself in a problem's technical details,
 * by the code the backend sends (`UnheardConversation.note`,
 * `Problem.note`). Placeholders: `{limit}`, `{holder}`.
 * @i18n-keys
 */
export const SYNC_PROBLEM_NOTES = {
  channel_without_team: "The rule names this channel without its team: pick it again in the rule.",
  past_channel_cap: "A rule answering in every channel follows the first {limit} channels of the account: name this channel in a rule to hear it.",
  team_channels_unlisted: "The provider refused to list the channels of this team to the account.",
  no_public_address: "Channels are only heard through push notifications, which need a public HTTPS BASE_URL.",
  operated: "Another user's agent account already operates this platform account: one agent account per platform account.",
  operated_own: "Your agent account {holder} already operates this platform account: one agent account per platform account. Add rules to that one, or disable it.",
} as const;

/** The technical details of a problem: the platform's notes in the viewer's language, then the provider's words as they are. */
export function syncProblemDetails(
  problem: Pick<AgentAccountSyncProblem, "details" | "notes">,
  t: (key: string, values?: Record<string, string | number>) => string,
): string[] {
  const notes = (problem.notes ?? [])
    .filter((note) => note.code in SYNC_PROBLEM_NOTES)
    .map((note) => t(SYNC_PROBLEM_NOTES[note.code as keyof typeof SYNC_PROBLEM_NOTES], note.values));
  return [...notes, ...problem.details];
}

/** The label of a conversation a problem affects; a team without a name still reads as a sentence. */
export function syncTargetLabel(
  target: AgentAccountSyncTarget,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  switch (target.covers) {
    case "chats":
      return t("All chats");
    case "inbox":
      return t("The inbox");
    case "team":
      return target.name ? t("Every channel of {team}", { team: target.name }) : t("Every channel of a team");
    default:
      return target.name || t("A channel");
  }
}

type SyncProblemTitleKey = keyof typeof SYNC_PROBLEM_TITLES;
type SyncProblemHintKey = keyof typeof SYNC_PROBLEM_HINTS;

/** The title and the remedy of a problem, and the values they read. */
export function syncProblemCopy(
  problem: Pick<AgentAccountSyncProblem, "kind" | "holder" | "permission">,
  providerLabel: string,
): { title: SyncProblemTitleKey; hint: SyncProblemHintKey; values: Record<string, string> } {
  const values = { provider: providerLabel, holder: problem.holder ?? "", permission: problem.permission ?? "" };
  const kind: AgentAccountSyncProblemKind = problem.kind in SYNC_PROBLEM_TITLES ? problem.kind : "error";
  const named = !!problem.holder;
  switch (kind) {
    case "operated":
      return named ? { title: "operated_own", hint: "operated_own", values } : { title: kind, hint: kind, values };
    case "taken_by_account":
      return named
        ? { title: "taken_by_own_account", hint: "taken_by_own_account", values }
        : { title: kind, hint: kind, values };
    case "taken_elsewhere":
      return { title: named ? "taken_elsewhere_named" : kind, hint: kind, values };
    case "refused":
      return { title: kind, hint: problem.permission ? "refused_permission" : kind, values };
    default:
      return { title: kind, hint: kind, values };
  }
}

/** The action a problem's card offers: the integration to sign in again, or the rules to change. */
export function syncProblemAction(kind: AgentAccountSyncProblemKind): "integration" | "rules" | null {
  if (kind === "integration" || kind === "reauthorize" || kind === "refused") return "integration";
  if (kind === "taken_by_account" || kind === "taken_elsewhere" || kind === "not_found" || kind === "not_subscribed")
    return "rules";
  return null;
}

/** How much of the account the last check left unheard: null when it heard every subscription. */
export function syncShortfall(report: AgentAccountSyncReport): { missing: number; total: number } | null {
  const missing = Math.max(0, report.wanted - report.heard);
  return report.wanted > 0 && missing > 0 ? { missing, total: report.wanted } : null;
}

/** The first *limit* conversations a problem affects, and how many more it lists. */
export function visibleTargets(
  targets: AgentAccountSyncTarget[],
  limit: number,
): { shown: AgentAccountSyncTarget[]; more: number } {
  const shown = targets.slice(0, limit);
  return { shown, more: targets.length - shown.length };
}

/** What the account did with a message, by the page's groups (`activity.GROUPS`). @i18n-keys */
export const ACTIVITY_GROUP_LABELS = {
  answered: "Answered",
  needs_owner: "Handed to you",
  awaiting_approval: "Waiting for approval",
  shadow: "Recorded (shadow)",
  silent: "The agent stayed silent",
  waiting: "Grace period",
  dropped: "Dropped or expired",
  already_handled: "Handled elsewhere",
  failed: "Failed",
  left: "Left alone",
} as const satisfies Record<AgentAccountActivityGroup, string>;

/** The groups in the order the charts stack them, what was said first. */
export const ACTIVITY_GROUPS = Object.keys(ACTIVITY_GROUP_LABELS) as AgentAccountActivityGroup[];

/** @i18n-keys */
export const ACTIVITY_PERIOD_LABELS = {
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
} as const satisfies Record<AgentAccountActivityPeriod, string>;

/** The Kind filter of the Messages sent tab. @i18n-keys */
export const SENT_KIND_LABELS = {
  reply: "Replies to a message",
  proactive: "Proactive sends",
} as const satisfies Record<AgentAccountSentKind, string>;

/**
 * The Status filter of the Messages sent tab: every status a sent message takes, worded
 * as the row's status badge (OUTCOME_LABELS) so a filter and a row never disagree. @i18n-keys
 */
export const SENT_STATUS_LABELS = {
  sent: "Sent",
  pending: "Waiting for approval",
  shadow: "Recorded (shadow)",
  failed: "Failed",
  rejected: "Rejected",
  expired: "Expired",
} as const satisfies Record<AgentAccountReply["status"], string>;

/**
 * The query the tab's former one-choice filter (`?show=`) asks for: the links of the
 * Decisions tab and older bookmarks still carry it, read as the Kind or Status filter.
 */
export function sentFilterQuery(filter: string | null | undefined): AgentAccountReplyFilters {
  switch (filter) {
    case "reply":
    case "proactive":
      return { kind: filter };
    case "failed":
    case "pending":
    case "shadow":
      return { status: [filter] };
    default:
      return {};
  }
}

/** The links of a proactive send to what produced it: the agent, the conversation, the run, the assignment. @i18n-keys */
export const SEND_ORIGIN_LINK_LABELS = {
  agent: "Agent",
  conversation: "Conversation",
  run: "Run",
  assignment: "Assignment",
} as const;

export type SendOriginLink = { key: keyof typeof SEND_ORIGIN_LINK_LABELS; href: string };

/** Where each id of a send's origin opens, in the order of the labels; an id left out is no link. */
export function sendOriginLinks(origin: AgentAccountSendOrigin | null | undefined): SendOriginLink[] {
  if (!origin) return [];
  const hrefs: Record<SendOriginLink["key"], string | null> = {
    agent: origin.agent_id ? `/agents/${encodeURIComponent(origin.agent_id)}` : null,
    conversation: origin.conversation_id ? `/chat?c=${encodeURIComponent(origin.conversation_id)}` : null,
    run: origin.run_id ? `/operations/executions/${encodeURIComponent(origin.run_id)}` : null,
    assignment: origin.assignment_id ? `/agents/assignments/${encodeURIComponent(origin.assignment_id)}` : null,
  };
  return (Object.keys(SEND_ORIGIN_LINK_LABELS) as SendOriginLink["key"][]).flatMap((key) => {
    const href = hrefs[key];
    return href ? [{ key, href }] : [];
  });
}

/** The outcome groups the Decisions tab filters on. @i18n-keys */
export const DECISION_GROUP_FILTER_LABELS = {
  all: "All",
  answered: "Answered",
  needs_owner: "Handed to you",
  awaiting_approval: "Waiting for approval",
  silent: "Silent",
  already_handled: "Handled elsewhere",
  left: "Left alone",
  failed: "Failed",
} as const;

/** The label key of what became of a message: an outcome, else the decision's reason; null for an unknown code. */
export function outcomeLabelKey(outcome: string, kind: AgentAccountProviderKind = "chat"): string | null {
  if (kind === "mailbox" && outcome in MAILBOX_REASON_LABELS) {
    return MAILBOX_REASON_LABELS[outcome as keyof typeof MAILBOX_REASON_LABELS];
  }
  if (outcome in OUTCOME_LABELS) return OUTCOME_LABELS[outcome as keyof typeof OUTCOME_LABELS];
  if (outcome in DECISION_REASON_LABELS) return DECISION_REASON_LABELS[outcome as keyof typeof DECISION_REASON_LABELS];
  return null;
}

/** The badge tone of an activity group. */
export function groupTone(group: AgentAccountActivityGroup): Tone {
  if (group === "answered") return "success";
  if (group === "awaiting_approval" || group === "waiting" || group === "needs_owner") return "warning";
  if (group === "failed") return "destructive";
  if (group === "shadow") return "cyan";
  // Taken by another pipeline of the agent: an outline, so it never
  // reads as "Left alone" by colour alone.
  if (group === "already_handled") return "outline";
  return "secondary";
}

/** The share of the messages for the agent it answered, in percent; null when none was for it. */
export function answerRate(activity: Pick<AgentAccountActivity, "for_agent" | "groups">): number | null {
  if (!activity.for_agent) return null;
  return Math.round((activity.groups.answered / activity.for_agent) * 100);
}

/** The reasons messages were left alone, most frequent first, each with its label key (the code when unknown). */
export function leftReasons(
  activity: Pick<AgentAccountActivity, "left_reasons">,
  kind: AgentAccountProviderKind = "chat",
): { code: string; label: string; count: number }[] {
  return Object.entries(activity.left_reasons)
    .map(([code, count]) => ({ code, label: outcomeLabelKey(code, kind) ?? code, count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code));
}

/** The timeline as chart rows: one per bucket, a count per group. */
export function timelineRows(
  activity: Pick<AgentAccountActivity, "timeline">,
): ({ at: string } & Record<AgentAccountActivityGroup, number>)[] {
  return activity.timeline.map((bucket) => ({
    at: bucket.at,
    ...(Object.fromEntries(ACTIVITY_GROUPS.map((group) => [group, bucket.counts[group] ?? 0])) as Record<
      AgentAccountActivityGroup,
      number
    >),
  }));
}

/** The groups the timeline holds at least one message of, in stacking order. */
export function activeGroups(activity: Pick<AgentAccountActivity, "groups">): AgentAccountActivityGroup[] {
  return ACTIVITY_GROUPS.filter((group) => (activity.groups[group] ?? 0) > 0);
}

/** ISO weekdays, Monday first. @i18n-keys */
export const DAY_LABELS = {
  1: "Mon",
  2: "Tue",
  3: "Wed",
  4: "Thu",
  5: "Fri",
  6: "Sat",
  7: "Sun",
} as const;

export const CONVERSATION_TYPES: AgentAccountConversationType[] = ["direct", "group", "channel"];
export const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7] as const;

/** A fresh rule: every message of the given kinds, answered when the account is mentioned. */
export function newRule(overrides: Partial<AgentAccountRule> = {}): AgentAccountRule {
  return {
    id: Math.random().toString(36).slice(2, 12),
    name: "",
    enabled: true,
    action: "respond",
    conversation_types: [...CONVERSATION_TYPES],
    conversations: [],
    senders: [],
    groups: [],
    exclude_senders: [],
    trigger: "mention",
    reply: "always",
    continue_threads: true,
    response_mode: null,
    ai_label: null,
    grace_period_seconds: null,
    instructions: "",
    access: null,
    ...overrides,
  };
}

/** The guardrail a public-channel rule starts with; the owner edits it, or has it generated. */
const PUBLIC_CHANNEL_GUARDRAIL =
  "You answer in a channel everyone in the company reads. Share only information that is already public inside the company. Never quote or summarize board, financial, legal or HR material, even if asked, and say politely that it is not something you can share here.";

/** Ready-made rules the editor offers, the engine's own defaults otherwise. */
export function ruleTemplate(
  kind: "monitor_channel" | "vip" | "mute" | "leadership" | "public_channel",
): AgentAccountRule {
  switch (kind) {
    case "monitor_channel":
      return newRule({ conversation_types: ["channel"], trigger: "any", reply: "decide" });
    case "vip":
      return newRule({ trigger: "any", response_mode: "auto" });
    case "mute":
      return newRule({ action: "ignore", trigger: "any" });
    case "leadership":
      // Full access for the groups the owner picks: direct and group chats.
      return newRule({ conversation_types: ["direct", "group"], trigger: "any" });
    case "public_channel":
      // Placed first, so an executive asking in a public channel gets this profile too.
      return newRule({
        conversation_types: ["channel"],
        trigger: "mention",
        instructions: PUBLIC_CHANNEL_GUARDRAIL,
        access: emptyAccess({ knowledge_base_ids: [], mcp_server_ids: [], integrations: [], memory: "off" }),
      });
  }
}

export type AccessListKey = Exclude<keyof AgentAccountRuleAccess, "memory">;

const ACCESS_LIST_KEYS: AccessListKey[] = [
  "integrations",
  "knowledge_base_ids",
  "mcp_server_ids",
  "builtin_tool_ids",
  "custom_tool_ids",
  "skill_ids",
];

/** @i18n-keys */
export const ACCESS_LIST_LABELS = {
  integrations: "Integrations",
  knowledge_base_ids: "Knowledge bases",
  mcp_server_ids: "MCP servers",
  builtin_tool_ids: "Built-in tools",
  custom_tool_ids: "Custom tools",
  skill_ids: "Skills",
} as const satisfies Record<AccessListKey, string>;

/** An access block narrowing nothing but what *overrides* says. */
function emptyAccess(overrides: Partial<AgentAccountRuleAccess> = {}): AgentAccountRuleAccess {
  return {
    integrations: null,
    knowledge_base_ids: null,
    mcp_server_ids: null,
    builtin_tool_ids: null,
    custom_tool_ids: null,
    skill_ids: null,
    memory: "on",
    ...overrides,
  };
}

/** Whether *id* of the agent's *all* stays usable under *access*. */
export function accessAllows(access: AgentAccountRuleAccess | null, key: AccessListKey, id: string): boolean {
  const list = access?.[key];
  return list == null || list.includes(id);
}

/**
 * *access* with *id* switched on or off among the agent's *all*.
 *
 * Every id on is stored as `null` - the agent's whole set, ids it gains later
 * included - and an access block that narrows nothing collapses to `null`,
 * the way the server stores it.
 */
export function toggleAccess(
  access: AgentAccountRuleAccess | null,
  key: AccessListKey,
  id: string,
  all: string[],
): AgentAccountRuleAccess | null {
  const current = access?.[key] ?? all;
  const next = current.includes(id) ? current.filter((v) => v !== id) : [...current, id];
  const covered = all.every((v) => next.includes(v));
  return normalizeAccess({ ...(access ?? emptyAccess()), [key]: covered ? null : all.filter((v) => next.includes(v)) });
}

/** *access* with its memory switch set. */
export function withMemory(access: AgentAccountRuleAccess | null, on: boolean): AgentAccountRuleAccess | null {
  return normalizeAccess({ ...(access ?? emptyAccess()), memory: on ? "on" : "off" });
}

function normalizeAccess(access: AgentAccountRuleAccess): AgentAccountRuleAccess | null {
  const narrows = access.memory === "off" || ACCESS_LIST_KEYS.some((key) => access[key] !== null);
  return narrows ? access : null;
}

type AgentCapabilityFields = Pick<
  Agent,
  | "integration_bindings"
  | "integration_template_bindings"
  | "knowledge_base_ids"
  | "mcp_server_ids"
  | "builtin_tool_ids"
  | "custom_tool_ids"
  | "skill_ids"
>;

/** The integration providers an agent uses: bound (`provider#2` is the provider), or per-user templates. */
export function agentIntegrationProviders(agent: Pick<Agent, "integration_bindings" | "integration_template_bindings">): string[] {
  const bound = Object.keys(agent.integration_bindings ?? {}).map((key) => key.split("#")[0]);
  return [...new Set([...bound, ...(agent.integration_template_bindings ?? [])])];
}

/**
 * What a rule's access can switch off: the agent's own capabilities, by list,
 * named from the entity lists the user reads (an id stays its own label when
 * the user cannot read the entity). Lists the agent has nothing in are left out.
 */
export function agentCapabilities(
  agent: AgentCapabilityFields,
  names: Partial<Record<AccessListKey, { id: string; name: string }[] | undefined>>,
): { key: AccessListKey; items: { id: string; label: string }[] }[] {
  const ids: Record<AccessListKey, string[]> = {
    integrations: agentIntegrationProviders(agent),
    knowledge_base_ids: agent.knowledge_base_ids ?? [],
    mcp_server_ids: agent.mcp_server_ids ?? [],
    builtin_tool_ids: agent.builtin_tool_ids ?? [],
    custom_tool_ids: agent.custom_tool_ids ?? [],
    skill_ids: agent.skill_ids ?? [],
  };
  return ACCESS_LIST_KEYS.map((key) => {
    const known = new Map((names[key] ?? []).map((entity) => [entity.id, entity.name]));
    return { key, items: ids[key].map((id) => ({ id, label: known.get(id) ?? id })) };
  }).filter((list) => list.items.length > 0);
}

/** How much of the agent a rule leaves, for its collapsed header: null when it leaves everything. */
export function accessSummary(access: AgentAccountRuleAccess | null): { narrowed: number; memoryOff: boolean } | null {
  if (!access) return null;
  return {
    narrowed: ACCESS_LIST_KEYS.filter((key) => access[key] !== null).length,
    memoryOff: access.memory === "off",
  };
}

/** *list* with the item at *index* moved by *delta*, unchanged at the ends. */
export function moveItem<T>(list: T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (target < 0 || target >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
}

export type Tone = "success" | "warning" | "destructive" | "secondary" | "cyan" | "outline";

/** The badge tone of a decision's outcome. */
export function outcomeTone(outcome: string): Tone {
  if (outcome === "sent") return "success";
  if (outcome === "pending" || outcome === "waiting" || outcome === "flagged" || outcome === "queued") return "warning";
  if (outcome === "failed") return "destructive";
  if (outcome === "shadow") return "cyan";
  return "secondary";
}

/** The badge tone of an account's status. */
export function statusTone(status: string): Tone {
  if (status === "active") return "success";
  if (status === "error") return "destructive";
  if (status === "setup") return "warning";
  return "secondary";
}

/** Whether a decision means a reply will exist, and how it goes out. */
export function decisionSummary(decision: AgentAccountDecision): {
  key: keyof typeof DECISION_REASON_LABELS | "unknown";
  answers: boolean;
} {
  const key = decision.reason in DECISION_REASON_LABELS
    ? (decision.reason as keyof typeof DECISION_REASON_LABELS)
    : "unknown";
  return { key, answers: decision.respond };
}

/** A rule's scope as short phrases, for its collapsed header; *available* are the provider's kinds. */
export function ruleScope(
  rule: AgentAccountRule,
  available: AgentAccountConversationType[] = CONVERSATION_TYPES,
): {
  types: AgentAccountConversationType[];
  conversations: number;
  people: number;
  groups: number;
  excluded: number;
} {
  const types = rule.conversation_types.length ? rule.conversation_types : [...CONVERSATION_TYPES];
  return {
    types: types.filter((type) => available.includes(type)),
    conversations: rule.conversations.length,
    people: rule.senders.length,
    groups: (rule.groups ?? []).length,
    excluded: rule.exclude_senders.length,
  };
}
