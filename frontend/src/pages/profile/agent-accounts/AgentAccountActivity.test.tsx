// An agent account's page: the overview says what the account did, the
// decisions say why, row by row, the messages sent say what was written -
// the replies and what the agent sent on its own.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type {
  AgentAccount,
  AgentAccountActivity,
  AgentAccountDecisionFilters,
  AgentAccountDecisionPage,
  AgentAccountProviderInfo,
  AgentAccountReplyFilters,
  AgentAccountReplyPage,
} from "@/types";
import { AgentAccountDecisionsTab } from "./AgentAccountDecisionsTab";
import { AgentAccountOverview } from "./AgentAccountOverview";
import { AgentAccountMessagesSentTab } from "./AgentAccountMessagesSentTab";
import { AgentAccountReplyCard } from "./AgentAccountReplyCard";

const NO_GROUPS = {
  answered: 0, needs_owner: 0, awaiting_approval: 0, shadow: 0, silent: 0, waiting: 0, dropped: 0, already_handled: 0,
  failed: 0, left: 0,
};

const ACTIVITY: AgentAccountActivity = {
  period: "7d",
  bucket: "day",
  timezone: "Europe/Paris",
  since: "2026-10-04T08:00:00+00:00",
  until: "2026-10-11T08:00:00+00:00",
  heard: 42,
  for_agent: 8,
  groups: { ...NO_GROUPS, answered: 6, silent: 2, left: 34 },
  outcomes: { sent: 6, silent: 2, application_author: 30, not_addressed: 4 },
  left_reasons: { application_author: 30, not_addressed: 4 },
  by_conversation_type: { channel: { ...NO_GROUPS, left: 30 }, direct: { ...NO_GROUPS, answered: 6, silent: 2, left: 4 } },
  timeline: [{ at: "2026-10-11T00:00:00", counts: { ...NO_GROUPS, answered: 6, silent: 2, left: 34 } }],
  conversations: [
    { conversation_id: "19:ops", conversation_name: "Engineering > Alerts", conversation_type: "channel", with_name: "Workflows", heard: 30, for_agent: 0, answered: 0 },
    { conversation_id: "chat-alice", conversation_name: null, conversation_type: "direct", with_name: "Alice", heard: 12, for_agent: 8, answered: 6 },
  ],
  senders: [{ sender: "alice-id", sender_name: "Alice", heard: 8, for_agent: 8, answered: 6 }],
  rules: [{ rule_id: "direct", rule_name: "Direct messages", matched: 8, answered: 6, silent: 2 }],
  replies: { sent: 6 },
  median_answer_seconds: 75,
  median_approval_seconds: null,
  ratings: { positive: 3, negative: 1 },
};

const DECISIONS: AgentAccountDecisionPage = {
  total: 42,
  items: [
    {
      id: "d1", at: new Date().toISOString(), updated_at: new Date().toISOString(), message_at: new Date().toISOString(),
      outcome: "sent", group: "answered", reason: "respond", respond: true, waited: true,
      conversation_type: "direct", conversation_id: "chat-alice", conversation_name: null,
      sender_id: "alice-id", sender_name: "Alice", preview: "Can you share the board deck?",
      mentions_account: false, addressed: true, thread_continuation: false,
      rule_id: "direct", rule_name: "Direct messages", sender: null, note: null,
      reply_id: "r1", reply_status: "sent", reply_text: "Here it is.", reply_error: null,
    },
    {
      id: "d2", at: new Date().toISOString(), updated_at: new Date().toISOString(), message_at: null,
      outcome: "application_author", group: "left", reason: "application_author", respond: false, waited: false,
      conversation_type: "channel", conversation_id: "19:ops", conversation_name: "Engineering > Alerts",
      sender_id: "app", sender_name: "Workflows", preview: "[FIRING:1] Disk full",
      mentions_account: false, addressed: false, thread_continuation: false,
      rule_id: null, rule_name: null, sender: null, note: null,
      reply_id: null, reply_status: null, reply_text: null, reply_error: null,
    },
  ],
};

/** A mailbox's log: an email handed to its owner, and one where the account was only in Cc. */
const MAIL_DECISIONS: AgentAccountDecisionPage = {
  total: 2,
  items: [
    {
      ...DECISIONS.items[0], id: "m1", outcome: "flagged", group: "needs_owner", waited: false,
      conversation_name: "Discount request", preview: "Can we get a 30% discount?", mentions_account: true,
      conversation_type: "group", note: "Pricing is the owner's call.", reply_id: null, reply_status: null, reply_text: null,
    },
    {
      ...DECISIONS.items[1], id: "m2", outcome: "not_addressed", reason: "not_addressed", conversation_type: "group",
      conversation_name: "Team lunch", preview: "Pizza on Friday?", sender_name: "Bob",
    },
  ],
};

const MAILBOX = {
  key: "microsoft_outlook",
  label: "Microsoft Outlook",
  integration_provider: "microsoft",
  kind: "mailbox",
  capabilities: { direct: true, group: true, channel: false, triage: true, conversations: false },
  push_reachable: true,
} as unknown as AgentAccountProviderInfo;

let decisionPage = DECISIONS;

const REPLIES: AgentAccountReplyPage = {
  total: 2,
  items: [
    {
      id: "r1", account_id: "acct", account_name: "My digital twin", provider: "microsoft_teams", status: "sent", conversation_type: "direct",
      conversation_name: null, sender_name: "Alice", sender_email: null, inbound_text: "Can you share the board deck?",
      draft_text: "Here it is.", sent_text: "Here it is.", rule_name: "Direct messages", ai_label: true, error: null,
      decided_at: null, created_at: new Date().toISOString(), rating_positive: 2, rating_negative: 0, kind: "reply",
    },
    {
      id: "p1", account_id: "acct", account_name: "My digital twin", provider: "microsoft_teams", status: "sent", conversation_type: "direct",
      conversation_name: "Bob", sender_name: "My digital twin", sender_email: null, inbound_text: "",
      draft_text: "The decision is taken: we go.", sent_text: "The decision is taken: we go.", rule_name: null, ai_label: false,
      error: null, decided_at: null, created_at: new Date().toISOString(), rating_positive: 0, rating_negative: 0,
      kind: "proactive", origin: { tool: "send_microsoft_chat_message", source: "chat", recipients: ["bob@example.com"] },
    },
  ],
};

const PROACTIVE_DECISION = {
  ...DECISIONS.items[0], id: "d3", outcome: "sent", group: "answered" as const, reason: null, waited: false,
  conversation_name: "Bob", sender_id: null, sender_name: "My digital twin", preview: "The decision is taken: we go.",
  rule_id: null, rule_name: null, reply_id: "p1", reply_text: "The decision is taken: we go.",
  kind: "proactive" as const, origin: { tool: "send_microsoft_chat_message", source: "chat", recipients: ["bob@example.com"] },
};

/** What the activity hook answers; a test swaps it and puts it back. */
let activity: AgentAccountActivity = ACTIVITY;
const decisionFilters: AgentAccountDecisionFilters[] = [];
const sentFilters: AgentAccountReplyFilters[] = [];
/** What the messages sent hook answers besides its page; a test swaps it and puts it back. */
let replyRead: { isError: boolean; refetch: () => void } = { isError: false, refetch: () => {} };
/** What the decisions hook answers besides its page; a test swaps it and puts it back. */
let decisionRead: { isError: boolean; refetch: () => void } = { isError: false, refetch: () => {} };

vi.mock("@/hooks/api/useAgentAccountApi", () => ({
  useAgentAccountActivity: () => ({ data: activity, isLoading: false }),
  useAgentAccountProvider: (key?: string | null) => (key === "microsoft_outlook" ? MAILBOX : undefined),
  useAgentAccountDecisionLog: (_id: string, filters: AgentAccountDecisionFilters) => {
    decisionFilters.push(filters);
    return decisionRead.isError
      ? { data: undefined, isLoading: false, isFetching: false, isError: true, refetch: decisionRead.refetch }
      : { data: decisionPage, isLoading: false, isFetching: false, isError: false, refetch: decisionRead.refetch };
  },
  useAgentAccountReplyPage: (_id: string, filters: AgentAccountReplyFilters) => {
    sentFilters.push(filters);
    return replyRead.isError
      ? { data: undefined, isLoading: false, isFetching: false, isError: true, refetch: replyRead.refetch }
      : { data: REPLIES, isLoading: false, isFetching: false, isError: false, refetch: replyRead.refetch };
  },
  useApproveAgentAccountReply: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRejectAgentAccountReply: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const ACCOUNT = { id: "acct", name: "My digital twin", can_manage: true } as AgentAccount;

function at(path: string, element: React.ReactElement, locale?: string) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/profile/agent-accounts/:accountId/:tab?" element={element} />
      </Routes>
    </MemoryRouter>,
    { locale },
  );
}

describe("the account page", () => {
  it("overviews what the account did, how fast, where and why it left messages alone", () => {
    at("/profile/agent-accounts/acct", <AgentAccountOverview account={ACCOUNT} />);
    expect(screen.getByText("Messages heard")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getByText("8 for the agent")).toBeTruthy();
    expect(screen.getByText("75% of the messages for the agent")).toBeTruthy();
    expect(screen.getByText("1m 15s")).toBeTruthy();
    expect(screen.getByText("Engineering > Alerts")).toBeTruthy();
    expect(screen.getByText("Direct message with Alice")).toBeTruthy();
    expect(screen.getByText("Alice")).toBeTruthy();
    // The rule's row, and the kind of conversation in the legend of "Kinds of conversation".
    expect(screen.getAllByText("Direct messages")).toHaveLength(2);
    expect(screen.getByText("Why messages were left alone")).toBeTruthy();
  });

  it("draws the rules, conversations and people on the shared data table, sortable and searchable", () => {
    at("/profile/agent-accounts/acct", <AgentAccountOverview account={ACCOUNT} />);
    // Three shared tables (rules, conversations, people), each with its own search box.
    expect(screen.getAllByTestId("data-table-scroll")).toHaveLength(3);
    expect(screen.getAllByRole("searchbox")).toHaveLength(3);
    expect(screen.getAllByRole("columnheader", { name: /Matched/ })).toHaveLength(1);
    expect(screen.getAllByRole("columnheader", { name: /For the agent/ })).toHaveLength(2);
    expect(screen.getByText("Engineering > Alerts")).toBeTruthy();
  });

  it("opens a conversation's decisions through a real link, reachable from the keyboard", () => {
    at("/profile/agent-accounts/acct", <AgentAccountOverview account={ACCOUNT} />);
    const link = screen.getByText("Engineering > Alerts").closest("a");
    expect(link).not.toBeNull();
    const href = link?.getAttribute("href") ?? "";
    expect(href.startsWith("/profile/agent-accounts/acct/decisions?")).toBe(true);
    expect(new URLSearchParams(href.split("?")[1]).get("label")).toBe("Engineering > Alerts");
  });

  it("lists every decision and opens one on why it happened and what was replied", () => {
    at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
    // The shared table draws one pager, in its footer (#10215 criterion 6), and the
    // total once, in its toolbar (Sam, 11 Nov 2026).
    expect(screen.getAllByText("Page 1 of 2")).toHaveLength(1);
    expect(screen.getByTestId("data-table-total").textContent).toBe("42");
    expect(screen.queryByText(/Showing \d/)).toBeNull();
    expect(screen.getByText("An app or a bot wrote it")).toBeTruthy();
    expect(screen.getByText("after a grace period")).toBeTruthy();
    expect(screen.queryByText("Here it is.")).toBeNull();
    fireEvent.click(screen.getByText("Can you share the board deck?"));
    expect(screen.getByText("Here it is.")).toBeTruthy();
    expect(screen.getByText("A rule answers it")).toBeTruthy();
    expect(screen.getByText("A direct message")).toBeTruthy();
  });

  it("spans the opened decision over the columns on screen, Rule hidden below xl", () => {
    at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
    const headers = screen.getAllByRole("columnheader");
    expect(headers).toHaveLength(5);
    expect(screen.queryByRole("columnheader", { name: "Rule" })).toBeNull();
    // Fixed layout (UX round 1 on #9911, B1): the names keep their width, Message takes the rest.
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
    expect(headers.map((header) => header.style.width)).toEqual(["144px", "160px", "160px", "112px", ""]);
    fireEvent.click(screen.getByText("Can you share the board deck?"));
    const detail = screen.getByText("Here it is.").closest("td");
    expect(detail?.getAttribute("colspan")).toBe(String(headers.length));
  });

  it("shows the Rule column from xl and spans the opened decision over 6 columns", () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query === "(min-width: 1280px)",
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
      const headers = screen.getAllByRole("columnheader");
      expect(headers).toHaveLength(6);
      expect(screen.getByRole("columnheader", { name: "Rule" })).toBeTruthy();
      expect(headers.map((header) => header.style.width)).toEqual(["144px", "192px", "224px", "160px", "", "160px"]);
      fireEvent.click(screen.getByText("Can you share the board deck?"));
      const detail = screen.getByText("Here it is.").closest("td");
      expect(detail?.getAttribute("colspan")).toBe(String(headers.length));
    } finally {
      window.matchMedia = original;
    }
  });

  it("draws the decisions on the shared data table: search, outcome and kind filters, sortable columns", () => {
    decisionFilters.length = 0;
    at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
    expect(screen.getAllByTestId("data-table-scroll")).toHaveLength(1);
    expect(screen.getByRole("searchbox", { name: "Search decisions..." })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /Outcome/ }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: /Kind of conversation/ }).length).toBeGreaterThan(0);
    const latest = () => decisionFilters[decisionFilters.length - 1];
    expect(latest()).toMatchObject({ sort: "date", direction: "desc" });
    fireEvent.click(screen.getByRole("button", { name: /Sent by/ }));
    expect(latest()).toMatchObject({ sort: "sender", direction: "asc" });
  });

  it("exports the decisions as CSV and keeps each row on one line, every wording in a title", () => {
    at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
    expect(screen.getByRole("button", { name: /Export CSV/ })).toBeTruthy();
    const cells = screen.getAllByTestId("decision-outcome-cell");
    expect(cells.map((cell) => cell.getAttribute("title"))).toEqual([
      "Sent - after a grace period",
      "An app or a bot wrote it",
    ]);
    // The message is one cut line with its whole text in a title; the opened row shows it all.
    expect(screen.getByTitle("Can you share the board deck?")).toBeTruthy();
  });

  it("draws a failed read of the decisions as the table's error row with Retry, the tab still on screen", () => {
    const refetch = vi.fn();
    decisionRead = { isError: true, refetch };
    try {
      at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
      expect(screen.getByRole("alert")).toBeTruthy();
      expect(screen.getByRole("searchbox")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(refetch).toHaveBeenCalledTimes(1);
    } finally {
      decisionRead = { isError: false, refetch: () => {} };
    }
  });

  it("reads the decisions without the app-wide throwOnError, so a 500 never reaches the section boundary", () => {
    const hooks = readFileSync(join(process.cwd(), "src/hooks/api/useAgentAccountApi.ts"), "utf8");
    const start = hooks.indexOf("export function useAgentAccountDecisionLog");
    const end = hooks.indexOf("\nexport function ", start + 1);
    expect(start).toBeGreaterThan(-1);
    expect(hooks.slice(start, end)).toContain("throwOnError: false");
  });

  it("opens a decision from the keyboard through its expand button", () => {
    at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
    const expand = screen.getAllByRole("button", { name: "Expand" })[0];
    expect(expand.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(expand);
    expect(screen.getByText("Here it is.")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Collapse" })[0].getAttribute("aria-expanded")).toBe("true");
  });

  it("filters the decisions from the link the overview opens", () => {
    decisionFilters.length = 0;
    at(
      "/profile/agent-accounts/acct/decisions?conversation=19%3Aops&label=Engineering%20%3E%20Alerts&group=left",
      <AgentAccountDecisionsTab account={ACCOUNT} />,
    );
    const latest = () => decisionFilters[decisionFilters.length - 1];
    expect(latest()).toMatchObject({ conversationId: "19:ops", groups: ["left"] });
    expect(screen.getByText("In conversation")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Clear"));
    expect(latest().conversationId).toBeUndefined();
  });

  it("lists every message sent on the shared table, and opens a row on the whole message", () => {
    at("/profile/agent-accounts/acct/sent", <AgentAccountMessagesSentTab account={ACCOUNT} />);
    expect(screen.getAllByTestId("data-table-scroll")).toHaveLength(1);
    // One row per message: what was written, its status, a proactive send marked as such.
    expect(screen.getByText("Here it is.")).toBeTruthy();
    expect(screen.getByText("The decision is taken: we go.")).toBeTruthy();
    expect(screen.getAllByText("Sent").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("img", { name: "Proactive send" })).toBeTruthy();
    expect(screen.queryByText("Posted")).toBeNull();
    // The row opens in place on the whole message: the ratings, the recipients and the origin.
    const [first, second] = screen.getAllByRole("button", { name: "Expand" });
    fireEvent.click(first);
    // The toolbar total also reads "2" (standard v2, #10916): the rating is the other one.
    // The chip keeps its reserved width loaded too (#11349), so its figure sits in an inner span.
    expect(screen.getAllByText("2").filter((el) => !el.closest('[data-testid="data-table-total"]'))).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Collapse" })[0].getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(second);
    expect(screen.getByText(/To: bob@example\.com/)).toBeTruthy();
    expect(screen.getAllByText("Posted")).toHaveLength(2);
    // The origin is carried by translated links, never by the raw run source or tool name.
    expect(screen.queryByText(/send_microsoft_chat_message/)).toBeNull();
  });

  it("links a proactive send to the agent, conversation, run and assignment that produced it", () => {
    const full = {
      ...REPLIES.items[1], id: "p2", sent_text: "Opening message.", draft_text: "Opening message.",
      origin: { tool: "send_microsoft_chat_message", agent_id: "ag-1", conversation_id: "cv-1", run_id: "rn-1", assignment_id: "as-1" },
    };
    at("/profile/agent-accounts/acct/sent", <AgentAccountReplyCard reply={full} />);
    const hrefs = screen.getAllByRole("link", { name: /that produced this send/ }).map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["/agents/ag-1", "/chat?c=cv-1", "/operations/executions/rn-1", "/agents/assignments/as-1"]);
  });

  it("renders no origin link when the origin names no id", () => {
    at("/profile/agent-accounts/acct/sent", <AgentAccountReplyCard reply={{ ...REPLIES.items[1], id: "p3", origin: { tool: "t" } }} />);
    expect(screen.queryAllByRole("link", { name: /that produced this send/ })).toHaveLength(0);
  });

  it.each([
    ["fr", "Rechercher dans les messages envoyés..."],
    ["ja", "送信済みメッセージを検索..."],
  ])("names the messages sent search in %s", (locale, name) => {
    at("/profile/agent-accounts/acct/sent", <AgentAccountMessagesSentTab account={ACCOUNT} />, locale);
    expect(screen.getByRole("searchbox", { name })).toBeTruthy();
    expect(screen.queryByRole("searchbox", { name: "Search the messages sent..." })).toBeNull();
  });

  it("filters the messages sent by kind and by any of several statuses, and sorts them on the server", () => {
    sentFilters.length = 0;
    at("/profile/agent-accounts/acct/sent", <AgentAccountMessagesSentTab account={ACCOUNT} />);
    const latest = () => sentFilters[sentFilters.length - 1];
    const pill = (name: RegExp) =>
      screen.getAllByRole("button", { name }).find((button) => button.hasAttribute("aria-expanded")) as HTMLElement;
    expect(latest()).toMatchObject({ sort: "date", direction: "desc" });
    expect(latest().kind).toBeUndefined();
    expect(latest().status).toBeUndefined();
    fireEvent.click(pill(/^Kind/));
    fireEvent.click(screen.getByRole("button", { name: "Proactive sends" }));
    expect(latest()).toMatchObject({ kind: "proactive" });
    // Both kinds is every message: the API is asked for none.
    fireEvent.click(screen.getByRole("button", { name: "Replies to a message" }));
    expect(latest().kind).toBeUndefined();
    fireEvent.click(pill(/^Status/));
    // Worded as the row's status badge, so a filter and a row never disagree.
    for (const label of ["Sent", "Waiting for approval", "Recorded (shadow)", "Failed", "Rejected", "Expired"]) {
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    }
    fireEvent.click(screen.getByRole("button", { name: "Failed" }));
    fireEvent.click(screen.getByRole("button", { name: "Waiting for approval" }));
    expect(latest().status).toEqual(["failed", "pending"]);
    fireEvent.click(screen.getByRole("button", { name: /Conversation/ }));
    expect(latest()).toMatchObject({ sort: "conversation", direction: "asc" });
  });

  it("exports the messages sent as CSV and keeps each row on one line, its whole status in a title", () => {
    at("/profile/agent-accounts/acct/sent", <AgentAccountMessagesSentTab account={ACCOUNT} />);
    expect(screen.getByRole("button", { name: /Export CSV/ })).toBeTruthy();
    const cells = screen.getAllByTestId("sent-status-cell");
    expect(cells.map((cell) => cell.getAttribute("title"))).toEqual(["Sent", "Sent - Proactive send"]);
    expect(screen.getByTitle("The decision is taken: we go.")).toBeTruthy();
  });

  it.each([
    ["below xl", false, ["144px", "200px", "160px", ""]],
    ["from xl", true, ["144px", "240px", "192px", "", "160px"]],
  ] as const)("never cuts the status badge %s, Proactive send a named mark below xl", (_label, wide, widths) => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: wide && query === "(min-width: 1280px)",
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      at("/profile/agent-accounts/acct/sent", <AgentAccountMessagesSentTab account={ACCOUNT} />);
      const headers = screen.getAllByRole("columnheader");
      expect(headers.map((header) => header.style.width)).toEqual([...widths]);
      // The status wording is the badge's own text, never inside a cut span.
      for (const cell of screen.getAllByTestId("sent-status-cell")) {
        expect(cell.firstElementChild?.textContent).toBe("Sent");
        expect(cell.firstElementChild?.children).toHaveLength(0);
      }
      if (wide) {
        expect(screen.getByText("Proactive send")).toBeTruthy();
        expect(screen.queryByRole("img", { name: "Proactive send" })).toBeNull();
      } else {
        expect(screen.getByRole("img", { name: "Proactive send" })).toBeTruthy();
        expect(screen.queryByText("Proactive send")).toBeNull();
      }
    } finally {
      window.matchMedia = original;
    }
  });

  it("draws a failed read as the table's error row with Retry, the tab still on screen", () => {
    const refetch = vi.fn();
    replyRead = { isError: true, refetch };
    try {
      at("/profile/agent-accounts/acct/sent", <AgentAccountMessagesSentTab account={ACCOUNT} />);
      expect(screen.getByRole("alert")).toBeTruthy();
      expect(screen.getByRole("searchbox")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(refetch).toHaveBeenCalledTimes(1);
    } finally {
      replyRead = { isError: false, refetch: () => {} };
    }
  });

  it("reads the messages sent without the app-wide throwOnError, so a 500 never reaches the section boundary", () => {
    const hooks = readFileSync(join(process.cwd(), "src/hooks/api/useAgentAccountApi.ts"), "utf8");
    const start = hooks.indexOf("export function useAgentAccountReplyPage");
    const end = hooks.indexOf("\nexport function ", start + 1);
    expect(start).toBeGreaterThan(-1);
    expect(hooks.slice(start, end)).toContain("throwOnError: false");
  });

  it("reads the former one-choice filter of a link as the Kind or Status filter", () => {
    sentFilters.length = 0;
    at("/profile/agent-accounts/acct/sent?show=proactive", <AgentAccountMessagesSentTab account={ACCOUNT} />);
    expect(sentFilters[sentFilters.length - 1]).toMatchObject({ kind: "proactive" });
    cleanup();
    at("/profile/agent-accounts/acct/sent?show=pending", <AgentAccountMessagesSentTab account={ACCOUNT} />);
    expect(sentFilters[sentFilters.length - 1]).toMatchObject({ status: ["pending"] });
  });

  it("opens a proactive send of the log on what was sent, not on why", () => {
    decisionPage = { total: 1, items: [PROACTIVE_DECISION] };
    try {
      at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
      fireEvent.click(screen.getByText("The decision is taken: we go."));
      expect(screen.getAllByText("Proactive send").length).toBeGreaterThanOrEqual(2);
      expect(screen.getByText("bob@example.com")).toBeTruthy();
      expect(screen.queryByText("Addressed")).toBeNull();
      expect(screen.queryByText("Grace period")).toBeNull();
      expect(screen.getByText("See all messages sent")).toBeTruthy();
    } finally {
      decisionPage = DECISIONS;
    }
  });

  it("says a queued message waits for the current reply and a coalesced one was answered", () => {
    const queued = {
      ...DECISIONS.items[0], id: "q1", outcome: "queued", group: "waiting" as const, waited: false,
      preview: "And the budget?", reply_id: null, reply_status: null, reply_text: null,
    };
    const coalesced = {
      ...DECISIONS.items[0], id: "c1", outcome: "coalesced", group: "answered" as const, waited: false,
      preview: "Also the roadmap.", reply_id: null, reply_status: null, reply_text: null,
    };
    decisionPage = { total: 2, items: [queued, coalesced] };
    try {
      at("/profile/agent-accounts/acct/decisions", <AgentAccountDecisionsTab account={ACCOUNT} />);
      expect(screen.getByText("Queued")).toBeTruthy();
      expect(screen.getByText("Answered together")).toBeTruthy();
      fireEvent.click(screen.getByText("And the budget?"));
      expect(screen.getByText("It is answered once the current reply is posted.")).toBeTruthy();
      expect(screen.queryByText("The grace period is not over yet.")).toBeNull();
      fireEvent.click(screen.getByText("Also the roadmap."));
      expect(screen.getByText("Answered together with a later message of this conversation.")).toBeTruthy();
      expect(screen.queryByText("No reply: the message was not for the agent.")).toBeNull();
    } finally {
      decisionPage = DECISIONS;
    }
  });
});

describe("a mailbox's page", () => {
  const MAIL_ACCOUNT = { id: "mail", name: "Mailbox", provider: "microsoft_outlook", can_manage: true } as AgentAccount;

  it("speaks of emails, shows what the agent handed over and why", () => {
    decisionPage = MAIL_DECISIONS;
    try {
      at("/profile/agent-accounts/mail/decisions", <AgentAccountDecisionsTab account={MAIL_ACCOUNT} />);
      // The kinds of conversation are the options of the toolbar's filter pill.
      fireEvent.click(screen.getByRole("button", { name: /Kind of conversation/ }));
      expect(screen.getByText("Emails to the account alone")).toBeTruthy();
      expect(screen.queryByText("Channels")).toBeNull();
      // The row's badge; the Outcome pill holds the same wording among its options.
      expect(screen.getAllByText("Handed to you").length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText("The account is not in To")).toBeTruthy();
      fireEvent.click(screen.getByText("Can we get a 30% discount?"));
      expect(screen.getByText("Pricing is the owner's call.")).toBeTruthy();
      expect(screen.getByText("The account was in To")).toBeTruthy();
      expect(
        screen.getByText("The agent left it to you: nothing was sent, and it is waiting for your answer."),
      ).toBeTruthy();
    } finally {
      decisionPage = DECISIONS;
    }
  });

  it("shows a Handled elsewhere card only when another pipeline took mail", () => {
    at("/profile/agent-accounts/mail", <AgentAccountOverview account={MAIL_ACCOUNT} />);
    expect(screen.queryByText("Another pipeline of the agent took them")).toBeNull();
    cleanup();
    activity = { ...ACTIVITY, groups: { ...ACTIVITY.groups, already_handled: 3 } };
    try {
      at("/profile/agent-accounts/mail", <AgentAccountOverview account={MAIL_ACCOUNT} />);
      expect(screen.getByText("Another pipeline of the agent took them")).toBeTruthy();
    } finally {
      activity = ACTIVITY;
    }
  });

  it("writes the decision panel dates in the viewer's language", () => {
    decisionPage = {
      total: 1,
      items: [{ ...DECISIONS.items[0], message_at: "2026-11-03T10:54:00Z", updated_at: "2026-11-03T10:56:00Z" }],
    };
    try {
      render(
        <QueryClientProvider client={new QueryClient()}>
          <IntlProvider locale="fr" messages={{}} timeZone="UTC" onError={() => {}}>
            <MemoryRouter initialEntries={["/profile/agent-accounts/acct/decisions"]}>
              <Routes>
                <Route path="/profile/agent-accounts/:accountId/:tab?" element={<AgentAccountDecisionsTab account={ACCOUNT} />} />
              </Routes>
            </MemoryRouter>
          </IntlProvider>
        </QueryClientProvider>,
      );
      fireEvent.click(screen.getByText("Can you share the board deck?"));
      expect(screen.getByText("3 nov., 10:54")).toBeTruthy();
      expect(screen.getByText("3 nov., 10:56")).toBeTruthy();
      expect(screen.queryByText(/Nov 3/)).toBeNull();
    } finally {
      decisionPage = DECISIONS;
    }
  });

  it("counts the emails handed to the owner on the overview", () => {
    at("/profile/agent-accounts/mail", <AgentAccountOverview account={MAIL_ACCOUNT} />);
    expect(screen.getByText("Handed to you")).toBeTruthy();
    expect(screen.getByText("The agent left them to you")).toBeTruthy();
  });
});
