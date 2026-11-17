/**
 * Unit tests for the agent-account editor helpers - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ACTIVITY_GROUPS,
  ACTIVITY_GROUP_LABELS,
  ADDRESSING_LABELS,
  DECISION_GROUP_FILTER_LABELS,
  DECISION_REASON_LABELS,
  MAILBOX_CONVERSATION_TYPE_LABELS,
  MAILBOX_REASON_LABELS,
  OUTCOME_LABELS,
  providerConversationTypes,
  rulesForProvider,
  wording,
  SENDER_MATCH_LABELS,
  activeGroups,
  answerRate,
  groupTone,
  leftReasons,
  outcomeLabelKey,
  timelineRows,
  SYNC_PROBLEM_HINTS,
  SYNC_PROBLEM_NOTES,
  SYNC_PROBLEM_TITLES,
  accessAllows,
  accessSummary,
  agentCapabilities,
  agentIntegrationProviders,
  decisionSummary,
  moveItem,
  newRule,
  outcomeTone,
  ruleScope,
  ruleTemplate,
  senderMatch,
  statusTone,
  syncProblemAction,
  syncProblemCopy,
  syncProblemDetails,
  syncShortfall,
  syncTargetLabel,
  toggleAccess,
  visibleTargets,
  withMemory,
} from "./agent-accounts.ts";
import type { AgentAccountSyncProblemKind } from "../../../types/index.ts";

const KINDS: AgentAccountSyncProblemKind[] = [
  "integration",
  "reauthorize",
  "operated",
  "no_public_address",
  "unreachable",
  "refused",
  "taken_by_account",
  "taken_elsewhere",
  "not_found",
  "not_subscribed",
  "throttled",
  "unavailable",
  "error",
  "reactions_refused",
];

test("every sync problem kind has a title and a remedy, whatever it names", () => {
  for (const kind of KINDS) {
    for (const holder of [null, "My digital twin"]) {
      for (const permission of [null, "ChannelMessage.Read.All"]) {
        const copy = syncProblemCopy({ kind, holder, permission }, "Microsoft Teams");
        assert.ok(SYNC_PROBLEM_TITLES[copy.title], `${kind} title`);
        assert.ok(SYNC_PROBLEM_HINTS[copy.hint], `${kind} hint`);
        // Every placeholder the copy reads has a value: an ICU message never renders `{holder}`.
        for (const text of [SYNC_PROBLEM_TITLES[copy.title], SYNC_PROBLEM_HINTS[copy.hint]]) {
          for (const [, name] of text.matchAll(/\{(\w+)\}/g)) assert.ok(copy.values[name], `${kind}: ${name}`);
        }
      }
    }
  }
});

test("a problem names its holder and its permission only when it has them", () => {
  const refused = syncProblemCopy({ kind: "refused", holder: null, permission: "Chat.Read" }, "Microsoft Teams");
  assert.deepEqual([refused.title, refused.hint], ["refused", "refused_permission"]);
  assert.equal(refused.values.permission, "Chat.Read");
  assert.equal(syncProblemCopy({ kind: "refused", holder: null, permission: null }, "Teams").hint, "refused");
  assert.equal(syncProblemCopy({ kind: "taken_by_account", holder: "EA", permission: null }, "Teams").title, "taken_by_own_account");
  assert.equal(syncProblemCopy({ kind: "taken_by_account", holder: null, permission: null }, "Teams").title, "taken_by_account");
  assert.equal(syncProblemCopy({ kind: "taken_elsewhere", holder: "staging.example.com", permission: null }, "Teams").title, "taken_elsewhere_named");
  assert.equal(syncProblemCopy({ kind: "operated", holder: "EA", permission: null }, "Teams").hint, "operated_own");
  // A kind this build does not know yet reads as the provider's error.
  const unknown = syncProblemCopy({ kind: "something_new" as AgentAccountSyncProblemKind, holder: null, permission: null }, "Teams");
  assert.deepEqual([unknown.title, unknown.hint], ["error", "error"]);
});

test("a problem offers the action that fixes it", () => {
  assert.equal(syncProblemAction("refused"), "integration");
  assert.equal(syncProblemAction("reauthorize"), "integration");
  assert.equal(syncProblemAction("taken_by_account"), "rules");
  assert.equal(syncProblemAction("not_found"), "rules");
  assert.equal(syncProblemAction("not_subscribed"), "rules");
  assert.equal(syncProblemAction("throttled"), null);
  assert.equal(syncProblemAction("unreachable"), null);
});

test("the headline counts the conversations the account does not hear", () => {
  const base = { checked_at: null, problems: [] };
  assert.deepEqual(syncShortfall({ ...base, wanted: 14, heard: 2 }), { missing: 12, total: 14 });
  assert.equal(syncShortfall({ ...base, wanted: 14, heard: 14 }), null);
  assert.equal(syncShortfall({ ...base, wanted: 0, heard: 0 }), null);
});

// A stand-in for t_i18n: the key with its values filled in, marked to show it went through the catalog.
const t = (key: string, values: Record<string, string | number> = {}) =>
  `[${key.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? `{${name}}`))}]`;

test("the platform's own reasons are worded from the catalogs, the provider's words left as they are", () => {
  const details = syncProblemDetails(
    {
      notes: [
        { code: "past_channel_cap", values: { limit: 50 } },
        { code: "operated_own", values: { holder: "EA" } },
        { code: "from_a_newer_build", values: {} },
      ],
      details: ["Microsoft Graph answered 404"],
    },
    t,
  );
  assert.deepEqual(details, [
    "[A rule answering in every channel follows the first 50 channels of the account: name this channel in a rule to hear it.]",
    "[Your agent account EA already operates this platform account: one agent account per platform account. Add rules to that one, or disable it.]",
    "Microsoft Graph answered 404",
  ]);
  // A report stored before the notes existed shows its details alone.
  assert.deepEqual(syncProblemDetails({ details: ["x"] }, t), ["x"]);
  // Every placeholder of a note is one the backend sends.
  const sent: Record<string, string[]> = { past_channel_cap: ["limit"], operated_own: ["holder"] };
  for (const [code, text] of Object.entries(SYNC_PROBLEM_NOTES)) {
    for (const [, name] of text.matchAll(/\{(\w+)\}/g)) assert.ok(sent[code]?.includes(name), `${code}: ${name}`);
  }
});

test("a conversation reads as a sentence, a team without a name included", () => {
  assert.equal(syncTargetLabel({ covers: "team", name: "Locked" }, t), "[Every channel of Locked]");
  assert.equal(syncTargetLabel({ covers: "team", name: null }, t), "[Every channel of a team]");
  assert.equal(syncTargetLabel({ covers: "team", name: "" }, t), "[Every channel of a team]");
  assert.equal(syncTargetLabel({ covers: "chats", name: null }, t), "[All chats]");
  assert.equal(syncTargetLabel({ covers: "inbox", name: null }, t), "[The inbox]");
  assert.equal(syncTargetLabel({ covers: "channel", name: "Ops > A" }, t), "Ops > A");
  assert.equal(syncTargetLabel({ covers: "channel", name: null }, t), "[A channel]");
});

test("the conversations of a problem show a few, then how many more", () => {
  const targets = Array.from({ length: 9 }, (_, i) => ({ covers: "channel" as const, name: `Ops > ${i}` }));
  const { shown, more } = visibleTargets(targets, 6);
  assert.equal(shown.length, 6);
  assert.equal(more, 3);
  assert.equal(visibleTargets(targets, targets.length).more, 0);
  assert.deepEqual(visibleTargets([], 6), { shown: [], more: 0 });
});

test("a new rule answers mentions everywhere, overrides left to the account", () => {
  const rule = newRule();
  assert.deepEqual(rule.conversation_types, ["direct", "group", "channel"]);
  assert.equal(rule.trigger, "mention");
  assert.equal(rule.reply, "always");
  assert.equal(rule.action, "respond");
  assert.equal(rule.response_mode, null);
  assert.equal(rule.ai_label, null);
  assert.notEqual(newRule().id, newRule().id);
});

test("the templates are the three shapes the editor offers", () => {
  const monitor = ruleTemplate("monitor_channel");
  assert.deepEqual([monitor.conversation_types, monitor.trigger, monitor.reply], [["channel"], "any", "decide"]);
  assert.equal(ruleTemplate("vip").response_mode, "auto");
  assert.equal(ruleTemplate("mute").action, "ignore");
});

test("moving a rule reorders it and stops at the ends", () => {
  assert.deepEqual(moveItem(["a", "b", "c"], 0, 1), ["b", "a", "c"]);
  assert.deepEqual(moveItem(["a", "b", "c"], 2, -2), ["c", "a", "b"]);
  const list = ["a", "b"];
  assert.equal(moveItem(list, 0, -1), list);
  assert.equal(moveItem(list, 1, 1), list);
});

test("tones follow what happened", () => {
  assert.equal(outcomeTone("sent"), "success");
  assert.equal(outcomeTone("pending"), "warning");
  assert.equal(outcomeTone("failed"), "destructive");
  assert.equal(outcomeTone("shadow"), "cyan");
  assert.equal(outcomeTone("something new"), "secondary");
  assert.equal(statusTone("error"), "destructive");
  assert.equal(statusTone("paused"), "secondary");
});

test("a reason the screen does not know is still shown", () => {
  const base = {
    respond: false, rule_id: null, rule_name: null, must_reply: false, addressed: false,
    thread_continuation: false, response_mode: "approval" as const, ai_label: false, grace_period_seconds: 0,
  };
  assert.deepEqual(decisionSummary({ ...base, reason: "rate_limited" }), { key: "rate_limited", answers: false });
  assert.deepEqual(decisionSummary({ ...base, reason: "from the future" }), { key: "unknown", answers: false });
  assert.ok(Object.keys(DECISION_REASON_LABELS).includes("owner_takeover"));
});

test("the sender line says which of the rules' groups the sender is in, or why none", () => {
  assert.equal(senderMatch(undefined), null);
  const board = [{ id: "g", name: "Executive Management" }];
  assert.deepEqual(senderMatch({ known: true, emails: ["sam@x.io", "Sam@corp.x.io"], groups: board }), {
    key: "member", values: { emails: "sam@x.io, Sam@corp.x.io", groups: "Executive Management" },
  });
  assert.equal(senderMatch({ known: true, emails: ["sam@x.io"], groups: [] })?.key, "outside");
  assert.equal(senderMatch({ known: false, emails: ["eve@x.io"], groups: [] })?.key, "unknown");
  assert.equal(senderMatch({ known: false, emails: [], groups: [] })?.key, "no_email");
  assert.ok(Object.keys(SENDER_MATCH_LABELS).length === 4);
});

test("a rule without types covers every type", () => {
  const scope = ruleScope(newRule({ conversation_types: [], senders: [{ id: "x" }], groups: [{ id: "g" }] }));
  assert.deepEqual(scope.types, ["direct", "group", "channel"]);
  assert.equal(scope.people, 1);
  assert.equal(scope.groups, 1);
});

test("a new rule leaves the agent everything; the public-channel template narrows it", () => {
  assert.equal(newRule().access, null);
  assert.deepEqual(newRule().groups, []);
  const channel = ruleTemplate("public_channel");
  assert.deepEqual(channel.conversation_types, ["channel"]);
  assert.equal(channel.access?.memory, "off");
  assert.deepEqual(channel.access?.knowledge_base_ids, []);
  assert.equal(channel.access?.builtin_tool_ids, null);
  assert.ok(channel.instructions.length > 0);
  assert.equal(ruleTemplate("leadership").access, null);
});

test("switching capabilities off and on again stores only what narrows", () => {
  const all = ["kb-board", "kb-public"];
  const off = toggleAccess(null, "knowledge_base_ids", "kb-board", all);
  assert.deepEqual(off?.knowledge_base_ids, ["kb-public"]);
  assert.equal(accessAllows(off, "knowledge_base_ids", "kb-board"), false);
  assert.equal(accessAllows(off, "mcp_server_ids", "anything"), true);
  // Every id back on is the agent's whole set again, and nothing narrowed is no block at all.
  assert.equal(toggleAccess(off, "knowledge_base_ids", "kb-board", all), null);
  const noMemory = withMemory(null, false);
  assert.equal(noMemory?.memory, "off");
  assert.equal(withMemory(noMemory, true), null);
  assert.deepEqual(accessSummary(withMemory(off, false)), { narrowed: 1, memoryOff: true });
  assert.equal(accessSummary(null), null);
});

test("the capabilities a rule can switch off are the agent's own, named when readable", () => {
  const agent = {
    integration_bindings: { microsoft: ["i-1"], "opencti#2": ["i-2"] },
    integration_template_bindings: ["microsoft", "jira"],
    knowledge_base_ids: ["kb-1", "kb-hidden"],
    mcp_server_ids: [],
    builtin_tool_ids: ["web_search"],
    custom_tool_ids: null,
    skill_ids: null,
  };
  assert.deepEqual(agentIntegrationProviders(agent), ["microsoft", "opencti", "jira"]);
  const lists = agentCapabilities(agent, { knowledge_base_ids: [{ id: "kb-1", name: "Public wiki" }] });
  assert.deepEqual(lists.map((list) => list.key), ["integrations", "knowledge_base_ids", "builtin_tool_ids"]);
  assert.deepEqual(lists[1].items, [{ id: "kb-1", label: "Public wiki" }, { id: "kb-hidden", label: "kb-hidden" }]);
});

// -- the account page -------------------------------------------------------

const GROUP_COUNTS = {
  answered: 3, needs_owner: 0, awaiting_approval: 1, shadow: 0, silent: 2, waiting: 0, dropped: 0, already_handled: 0,
  failed: 0, left: 6,
};

test("every activity group has a label, in the order the charts stack them", () => {
  assert.deepEqual(ACTIVITY_GROUPS, [
    "answered", "needs_owner", "awaiting_approval", "shadow", "silent", "waiting", "dropped", "already_handled",
    "failed", "left",
  ]);
  for (const group of ACTIVITY_GROUPS) assert.ok(ACTIVITY_GROUP_LABELS[group]);
  assert.equal(groupTone("answered"), "success");
  assert.equal(groupTone("needs_owner"), "warning");
  assert.equal(outcomeTone("flagged"), "warning");
  assert.equal(groupTone("failed"), "destructive");
  assert.equal(groupTone("left"), "secondary");
  // A mail another pipeline of the agent took: its own group, label and filter, not "Left alone".
  // Its badge differs from "Left alone" by more than colour.
  assert.equal(groupTone("already_handled"), "outline");
  assert.notEqual(groupTone("already_handled"), groupTone("left"));
  assert.equal(ACTIVITY_GROUP_LABELS.already_handled, "Handled elsewhere");
  assert.equal(DECISION_GROUP_FILTER_LABELS.already_handled, "Handled elsewhere");
  assert.equal(outcomeLabelKey("already_handled"), OUTCOME_LABELS.already_handled);
});

test("what became of a message reads as an outcome, else as the decision's reason", () => {
  assert.equal(outcomeLabelKey("sent"), OUTCOME_LABELS.sent);
  assert.equal(outcomeLabelKey("owner_replied"), OUTCOME_LABELS.owner_replied);
  assert.equal(outcomeLabelKey("application_author"), DECISION_REASON_LABELS.application_author);
  assert.equal(outcomeLabelKey("something_new"), null);
  assert.equal(outcomeLabelKey("flagged"), OUTCOME_LABELS.flagged);
  // One turn per chat: the backend's queued and coalesced outcomes read as such.
  assert.equal(outcomeLabelKey("queued"), "Queued");
  assert.equal(outcomeLabelKey("coalesced"), "Answered together");
});

// -- mailboxes --------------------------------------------------------

const OUTLOOK = { capabilities: { direct: true, group: true, channel: false, triage: true, conversations: false } };

test("a mailbox has emails to it alone and emails with others, no channel", () => {
  assert.deepEqual(providerConversationTypes(OUTLOOK), ["direct", "group"]);
  assert.deepEqual(providerConversationTypes(undefined), ["direct", "group", "channel"]);
  const scope = ruleScope(newRule({ conversation_types: [] }), providerConversationTypes(OUTLOOK));
  assert.deepEqual(scope.types, ["direct", "group"]);
});

test("a saved rule drops the conversation types its provider lacks, never all of them", () => {
  const legacy = newRule({ conversation_types: ["direct", "group", "channel"] });
  const channelOnly = newRule({ conversation_types: ["channel"] });
  const fine = newRule({ conversation_types: ["direct"] });
  const [sent, untouched, same] = rulesForProvider([legacy, channelOnly, fine], OUTLOOK);
  assert.deepEqual(sent, { ...legacy, conversation_types: ["direct", "group"] });
  assert.equal(untouched, channelOnly);
  assert.equal(same, fine);
  assert.deepEqual(rulesForProvider([legacy], undefined), [legacy]);
});

test("a mailbox is worded in emails and To, a chat in messages and mentions", () => {
  const mail = wording("mailbox");
  const chat = wording("chat");
  assert.equal(mail.types.direct, MAILBOX_CONVERSATION_TYPE_LABELS.direct);
  assert.equal(mail.trigger.mention, ADDRESSING_LABELS.trigger_mention_mailbox);
  assert.equal(mail.short.any, ADDRESSING_LABELS.short_any_mailbox);
  assert.equal(mail.how.none, ADDRESSING_LABELS.how_none_mailbox);
  assert.equal(chat.trigger.mention, "Only when the account is mentioned");
  assert.equal(chat.how.direct, "A direct message");
  assert.equal(wording(undefined).hint, chat.hint);
});

test("a mailbox says why it left an email in its own words", () => {
  assert.equal(outcomeLabelKey("not_addressed", "mailbox"), MAILBOX_REASON_LABELS.not_addressed);
  assert.equal(outcomeLabelKey("application_author", "mailbox"), MAILBOX_REASON_LABELS.application_author);
  assert.equal(outcomeLabelKey("owner_takeover", "mailbox"), DECISION_REASON_LABELS.owner_takeover);
  const reasons = leftReasons({ left_reasons: { not_addressed: 3 } }, "mailbox");
  assert.equal(reasons[0].label, MAILBOX_REASON_LABELS.not_addressed);
});

test("the answer rate is the share of the messages for the agent it answered", () => {
  assert.equal(answerRate({ for_agent: 4, groups: GROUP_COUNTS }), 75);
  assert.equal(answerRate({ for_agent: 0, groups: GROUP_COUNTS }), null);
});

test("the reasons messages were left alone come most frequent first, an unknown code as itself", () => {
  const reasons = leftReasons({ left_reasons: { not_addressed: 2, application_author: 5, brand_new: 2 } });
  assert.deepEqual(reasons.map((r) => [r.code, r.count]), [["application_author", 5], ["brand_new", 2], ["not_addressed", 2]]);
  assert.equal(reasons[0].label, DECISION_REASON_LABELS.application_author);
  assert.equal(reasons[1].label, "brand_new");
});

test("the timeline becomes one chart row per bucket, every group counted, and only the groups seen are drawn", () => {
  const rows = timelineRows({
    timeline: [
      { at: "2026-10-11T09:00:00", counts: { ...GROUP_COUNTS } },
      { at: "2026-10-11T10:00:00", counts: { answered: 1 } as typeof GROUP_COUNTS },
    ],
  });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].left, 6);
  assert.equal(rows[1].answered, 1);
  assert.equal(rows[1].left, 0);
  assert.deepEqual(activeGroups({ groups: GROUP_COUNTS }), ["answered", "awaiting_approval", "silent", "left"]);
});
