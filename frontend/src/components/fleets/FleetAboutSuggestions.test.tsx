// The About hero's Suggestions (#10246, #10536): no chip without a finding, one
// chip counting the visible ones, "No suggestions" with "N dismissed" when every
// finding is dismissed; a dialog where each finding reads its kind, member and
// fix with Fix with AI, Open and Dismiss (with an optional reason); Fix all with
// AI builds one numbered request; Dismiss all posts ids null; the dismissed view
// lists who, when and why with Restore and Restore all.
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FleetDismissedFinding, FleetDocFinding } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

import { FleetAboutSuggestions } from "./FleetAboutSuggestions";
import { applyText, dismissedText, dismissReasonLabel, findingKindLabel, findingRequest, findingsRequest, findingTexts } from "./fleet-documentation";

const dismissMutate = vi.fn();
const restoreMutate = vi.fn();
const applyMutate = vi.fn();
const ask = vi.fn();

vi.mock("@/hooks/usePlatformConfig", () => ({ useAgenticAIEnabled: () => true }));
vi.mock("@/lib/ask-assistant", () => ({ useAskAssistant: () => ask }));
vi.mock("@/hooks/api/useFleetApi", async (orig) => ({
  ...(await orig<typeof import("@/hooks/api/useFleetApi")>()),
  useDismissFleetDocFindings: () => ({ mutate: dismissMutate, isPending: false, isError: false, variables: undefined, error: null }),
  useRestoreFleetDocFindings: () => ({ mutate: restoreMutate, isPending: false, isError: false, variables: undefined, error: null }),
  useApplyFleetPracticeSuggestion: () => ({ mutate: applyMutate, isPending: false, isError: false, variables: undefined, error: null }),
}));

const finding = (over: Partial<FleetDocFinding> = {}): FleetDocFinding => ({
  id: "abc123def456",
  kind: "missing_automation",
  member: "Assistant",
  finding: "Its description names the weekly company health report; no automation runs it.",
  suggested_fix: "Add a weekly schedule for the company health report.",
  ...over,
});

const dismissedOne = (over: Partial<FleetDismissedFinding> = {}): FleetDismissedFinding => ({
  id: "d1",
  kind: "other",
  member: "Developer",
  subject: "merge at green",
  finding: "The Developer merges its own pull requests without a review.",
  at: "2026-11-09T10:00:00+00:00",
  by: "u1",
  by_name: "Ada Admin",
  reason: "Standing decision",
  ...over,
});

const t = (id: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((s, [k, v]) => s.replace(`{${k}}`, String(v)), id);

function renderChip(
  findings: FleetDocFinding[],
  dismissed: FleetDismissedFinding[] = [],
  agentIdOf = (name: string) => (name === "Assistant" ? "agent-1" : undefined),
) {
  const onOpenMembers = vi.fn();
  renderWithProviders(
    <MemoryRouter>
      <FleetAboutSuggestions fleetId="f1" findings={findings} dismissed={dismissed} agentIdOf={agentIdOf} onOpenMembers={onOpenMembers} />
    </MemoryRouter>,
  );
  return { onOpenMembers };
}

describe("FleetAboutSuggestions", () => {
  beforeEach(() => {
    dismissMutate.mockReset();
    restoreMutate.mockReset();
    applyMutate.mockReset();
    ask.mockReset();
  });

  it("shows nothing at all when there is no finding and no dismissal", () => {
    renderChip([]);
    expect(screen.queryByTestId("about-suggestions-chip")).toBeNull();
    expect(screen.queryByTestId("about-suggestions-none")).toBeNull();
  });

  it("counts the findings on one chip and lists each with its actions", () => {
    renderChip([finding(), finding({ id: "x2", kind: "no_gate_on_sensitive_action", member: "", finding: "Emails go out without approval." })]);
    fireEvent.click(screen.getByRole("button", { name: "2 suggestions" }));
    const items = within(screen.getByTestId("about-suggestions-list")).getAllByTestId("about-suggestion");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText("Automation missing, Assistant")).toBeTruthy();
    expect(within(items[0]).getByText("Suggested fix: Add a weekly schedule for the company health report.")).toBeTruthy();
    expect(within(items[0]).getByRole("button", { name: "Fix with AI" })).toBeTruthy();
    expect(within(items[0]).getByRole("link", { name: "Open" }).getAttribute("href")).toBe("/agents/agent-1");
    expect(within(items[1]).getByText("Sensitive action without approval")).toBeTruthy();
  });

  it("applies a back-consult suggestion after one confirmation, with no Fix with AI", () => {
    const backConsult = finding({
      id: "bc1",
      kind: "member_cannot_consult_orchestrator",
      member: "Triage",
      subject: "Desk Lead",
      finding: "Triage cannot consult Desk Lead for a route, scope or context question.",
      suggested_fix: "Add a Consult handover from Triage to Desk Lead.",
      source: "practice",
      applicable: true,
      agent_id: "agent-9",
    });
    renderChip([backConsult]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    const item = screen.getByTestId("about-suggestion");
    expect(within(item).getByText("Member cannot consult the orchestrator, Triage")).toBeTruthy();
    expect(within(item).getByText("Suggested fix: Add a Consult handover from Triage to Desk Lead.")).toBeTruthy();
    expect(within(item).queryByRole("button", { name: "Fix with AI" })).toBeNull();
    expect(within(item).getByRole("link", { name: "Open" }).getAttribute("href")).toBe("/agents/agent-9");
    fireEvent.click(within(item).getByTestId("about-suggestion-apply"));
    expect(applyMutate).not.toHaveBeenCalled();
    expect(screen.getByText("Triage gets a Consult handover to Desk Lead. Its other handovers keep their type.")).toBeTruthy();
    fireEvent.click(screen.getByTestId("about-suggestion-apply-confirm-button"));
    expect(applyMutate).toHaveBeenCalledTimes(1);
    expect(applyMutate.mock.calls[0][0]).toEqual({ id: "bc1", agentId: "agent-9" });
  });

  it("reads a practice suggestion in the reader's catalog, not the server's English", () => {
    const shown = findingTexts(
      finding({ kind: "member_chat_open", member: "Triage", finding: "server words", suggested_fix: "server fix", source: "practice" }),
      t,
    );
    expect(shown.finding).toBe("Triage takes chats. People usually reach a fleet through its orchestrator.");
    expect(findingTexts(finding(), t).finding).toBe(finding().finding);
    for (const catalog of [en, es, fr, ja] as Record<string, string>[]) {
      expect(catalog["{member} takes chats. People usually reach a fleet through its orchestrator."]).toBeTruthy();
      expect(catalog["Member cannot consult the orchestrator"]).toBeTruthy();
    }
  });

  it("words the knowledge practices and their Apply in every catalog (#11972)", () => {
    const grant = finding({
      kind: "orchestrator_cannot_write_knowledge",
      member: "Desk Lead",
      subject: "Alert Desk - Working knowledge",
      source: "practice",
      applicable: true,
    });
    expect(findingTexts(grant, t)).toEqual({
      finding: "The whole fleet reads Alert Desk - Working knowledge, and Desk Lead cannot write it.",
      fix: "Give Desk Lead write access to Alert Desk - Working knowledge.",
    });
    expect(applyText(grant, t)).toBe("Desk Lead gets write access to Alert Desk - Working knowledge. Its other knowledge bases keep their access.");
    const bind = finding({ kind: "member_missing_working_knowledge", member: "Report", subject: "Alert Desk - Working knowledge", source: "practice" });
    expect(applyText(bind, t)).toBe("Alert Desk - Working knowledge is bound to Report, read-only. Its other knowledge bases keep their access.");
    expect(applyText(finding({ kind: "fleet_without_working_knowledge", source: "practice" }), t)).toBe("");
    expect(findingKindLabel("several_writers_on_working_knowledge")).toBe("Knowledge base with several writers");
    for (const catalog of [en, es, fr, ja] as Record<string, string>[]) {
      expect(catalog["No working knowledge base"]).toBeTruthy();
      expect(catalog["The whole fleet reads {target}, and {member} cannot write it."]).toBeTruthy();
      expect(catalog["{target} is bound to {member}, read-only. Its other knowledge bases keep their access."]).toBeTruthy();
    }
  });

  it("names the orchestrator's listener events by their labels, never their keys, in every catalog (UX r1 B2)", () => {
    const listener = finding({ kind: "orchestrator_without_listeners", member: "Desk Lead", source: "practice" });
    expect(findingTexts(listener, t).finding).toBe("Desk Lead has no automation started by Work item completed, Run ended unfinished or Run failed.");
    for (const catalog of [en, es, fr, ja] as Record<string, string>[]) {
      const tc = (id: string, values?: Record<string, string | number>) => t(catalog[id] ?? id, values);
      const shown = findingTexts(listener, tc);
      expect(shown.finding).not.toMatch(/work_item\.|run\.(orphaned|failed)/);
      expect(catalog["{member} has no automation started by {first}, {second} or {third}."]).toBeTruthy();
    }
  });

  it("words a dismissed practice row in the reader's language, never the server's English (UX r2 B1)", () => {
    const listener = dismissedOne({
      kind: "orchestrator_without_listeners",
      member: "Desk Lead",
      subject: "",
      finding: "Desk Lead listens on none of work_item.completed, run.orphaned or run.failed.",
      source: "practice",
    });
    for (const catalog of [en, es, fr, ja] as Record<string, string>[]) {
      const tc = (id: string, values?: Record<string, string | number>) => t(catalog[id] ?? id, values);
      const line = dismissedText(listener, tc);
      expect(line).not.toMatch(/work_item\.|run\.(orphaned|failed)|listens on none/);
      expect(line).toContain("Desk Lead");
    }
    expect(dismissedText(dismissedOne(), t)).toBe(dismissedOne().finding);
    renderChip([], [listener]);
    fireEvent.click(screen.getByTestId("about-suggestions-dismissed-link"));
    expect(screen.getByTestId("about-dismissed-list").textContent).toContain(
      "Desk Lead has no automation started by Work item completed, Run ended unfinished or Run failed.",
    );
  });

  it("introduces practice and AI suggestions alike, never as AI findings only (UX r1 B1)", () => {
    renderChip([finding({ kind: "member_chat_open", member: "Triage", source: "practice" })]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    const dialog = screen.getByTestId("about-suggestions-dialog");
    expect(within(dialog).queryByText(/What the AI found/)).toBeNull();
    expect(within(dialog).getByText(/good practices checked each time this page opens/)).toBeTruthy();
  });

  it("renders the Apply and Dismiss confirmations with one shared button block (UX r1 B3)", () => {
    renderChip([
      finding({ id: "bc1", kind: "member_cannot_consult_orchestrator", member: "Triage", subject: "Desk Lead", source: "practice", applicable: true }),
      finding({ id: "x2" }),
    ]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByTestId("about-suggestion-apply"));
    const items = screen.getAllByTestId("about-suggestion");
    fireEvent.click(within(items[1]).getByRole("button", { name: "Dismiss suggestion" }));
    const blocks = screen.getAllByTestId("about-suggestion-inline-confirm");
    expect(blocks).toHaveLength(2);
    // Same block: Cancel first, then the confirm button, each enabled.
    for (const block of blocks) {
      const buttons = within(block).getAllByRole("button");
      expect(buttons).toHaveLength(2);
      expect(buttons[0].textContent).toBe("Cancel");
      expect(buttons.every((b) => !b.hasAttribute("disabled") && b.getAttribute("aria-disabled") === null)).toBe(true);
    }
  });

  it("reads one suggestion in the singular", () => {
    renderChip([finding()]);
    expect(screen.getByRole("button", { name: "1 suggestion" })).toBeTruthy();
  });

  it("hides the chip at 0 visible and offers the dismissed ones as a link", () => {
    renderChip([], [dismissedOne()]);
    expect(screen.queryByTestId("about-suggestions-chip")).toBeNull();
    const none = screen.getByTestId("about-suggestions-none");
    expect(within(none).getByText("No suggestions")).toBeTruthy();
    fireEvent.click(within(none).getByRole("button", { name: "1 dismissed" }));
    expect(screen.getByTestId("about-dismissed-list")).toBeTruthy();
  });

  it("dismisses one finding by its id with no reason by default", () => {
    renderChip([finding()]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    fireEvent.click(screen.getByTestId("about-suggestion-reason-confirm"));
    expect(dismissMutate).toHaveBeenCalledTimes(1);
    expect(dismissMutate.mock.calls[0][0]).toEqual({ ids: ["abc123def456"], reason: "" });
  });

  it("offers an optional reason, free text only on Other reason", () => {
    renderChip([finding()]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    expect(screen.queryByTestId("about-suggestion-reason-text")).toBeNull();
    expect(screen.getByRole("combobox", { name: "Reason (optional)" })).toBeTruthy();
  });

  it("Dismiss all asks once and posts ids null", () => {
    renderChip([finding(), finding({ id: "x2" }), finding({ id: "x3" })]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss all" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss 3 suggestions" }));
    expect(dismissMutate).toHaveBeenCalledTimes(1);
    expect(dismissMutate.mock.calls[0][0]).toEqual({ ids: null, reason: "" });
  });

  it("labels Dismiss with its own key, never the shared Close wording", () => {
    const label = (catalog: Record<string, string>) => catalog["Dismiss suggestion"];
    expect(label(en)).toBe("Dismiss");
    expect(label(fr)).toBe("Ignorer");
    expect(label(es)).toBe("Descartar");
    expect(label(ja)).toBe("非表示");
  });

  it("Fix with AI opens a chat with the request built from the suggested fix", () => {
    renderChip([finding()]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByRole("button", { name: "Fix with AI" }));
    expect(ask).toHaveBeenCalledTimes(1);
    const request = String(ask.mock.calls[0][0]);
    expect(request).toContain("fleet f1");
    expect(request).toContain("Assistant: Its description names the weekly company health report");
    expect(request).toContain("Suggested fix: Add a weekly schedule");
  });

  it("Fix all with AI opens one chat with every visible finding numbered", () => {
    renderChip([finding(), finding({ id: "x2", member: "", finding: "Emails go out without approval.", suggested_fix: "" })]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByRole("button", { name: "Fix all with AI" }));
    expect(ask).toHaveBeenCalledTimes(1);
    const request = String(ask.mock.calls[0][0]);
    expect(request).toContain("1. [abc123def456] Assistant: Its description names");
    expect(request).toContain("2. [x2] Emails go out without approval.");
    expect(request).toContain("list_fleet_doc_findings");
  });

  it("Open goes to the members view when the finding names no live member", () => {
    const { onOpenMembers } = renderChip([finding({ member: "Unknown" })]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(onOpenMembers).toHaveBeenCalledTimes(1);
  });

  it("shows dismissed findings with who and why, and restores one or all", () => {
    renderChip([finding()], [dismissedOne(), dismissedOne({ id: "d2", reason: "" })]);
    fireEvent.click(screen.getByTestId("about-suggestions-chip"));
    fireEvent.click(screen.getByRole("button", { name: "Show dismissed (2)" }));
    const rows = within(screen.getByTestId("about-dismissed-list")).getAllByTestId("about-dismissed");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText(/Dismissed by Ada Admin on/)).toBeTruthy();
    expect(within(rows[0]).getByText("Reason: Standing decision")).toBeTruthy();
    expect(within(rows[1]).queryByText(/Reason:/)).toBeNull();
    fireEvent.click(within(rows[0]).getByRole("button", { name: "Restore" }));
    expect(restoreMutate).toHaveBeenLastCalledWith({ ids: ["d1"] });
    fireEvent.click(screen.getByRole("button", { name: "Restore all" }));
    expect(restoreMutate).toHaveBeenLastCalledWith({ ids: null });
    fireEvent.click(screen.getByRole("button", { name: "Back to suggestions" }));
    expect(screen.getByTestId("about-suggestions-list")).toBeTruthy();
  });
});

describe("finding helpers", () => {
  it("labels every kind and falls back to Configuration", () => {
    expect(findingKindLabel("member_without_role")).toBe("Member without a role");
    expect(findingKindLabel("made_up")).toBe("Configuration");
  });

  it("builds a request without a fix line when none is suggested", () => {
    const text = findingRequest("f9", finding({ member: "", suggested_fix: "" }), t);
    expect(text).toContain("fleet f9");
    expect(text).not.toContain("Suggested fix");
  });

  it("builds one Fix all request with N items", () => {
    const text = findingsRequest("f9", [finding(), finding({ id: "b" }), finding({ id: "c" })], t);
    expect(text.split("\n").filter((line) => /^\d+\. \[/.test(line))).toHaveLength(3);
  });

  it("reads a known reason in the catalog and free text as written", () => {
    expect(dismissReasonLabel("False positive", t)).toBe("False positive");
    expect(dismissReasonLabel("decided in the weekly review", t)).toBe("decided in the weekly review");
  });
});
