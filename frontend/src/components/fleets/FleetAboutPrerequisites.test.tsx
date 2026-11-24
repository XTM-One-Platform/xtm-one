// The About page's derived prerequisites (#10258 S2, UX round 1 B1-B4): a
// complete fleet folds every line into "N met" with no Fix, a broken line says
// why in the reader's language with its Fix link, a fix the reader may not
// open says only an administrator can, every line wears a logo or kind icon,
// and the status band counts the checked lines and offers Fix with AI.
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { FleetLivePrerequisite } from "@/hooks/api/useFleetApi";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { renderWithProviders } from "@/testing/providers";

import { FleetAboutPrerequisites, FleetPrerequisitesChip } from "./FleetAboutPrerequisites";
import {
  PREREQUISITE_LABELS,
  PREREQUISITE_REASONS,
  failingLinesText,
  prerequisiteLabel,
  prerequisiteReason,
  prerequisiteSummary,
} from "./fleet-prerequisites";

vi.mock("@/hooks/usePlatformConfig", () => ({ useAgenticAIEnabled: () => true }));

const mutate = vi.fn();
vi.mock("@/hooks/api/useFleetApi", async (orig) => ({
  ...(await orig<typeof import("@/hooks/api/useFleetApi")>()),
  useConfirmFleetPrerequisite: () => ({ mutate, isPending: false, isError: false, variables: undefined, error: null }),
}));

const ask = vi.fn();
vi.mock("@/lib/ask-assistant", () => ({ useAskAssistant: () => ask }));

const line = (over: Partial<FleetLivePrerequisite>): FleetLivePrerequisite => ({
  check: "binding:1",
  kind: "integration",
  label: "GitHub ([Sam] GitHub)",
  state: "met",
  reason: "",
  member_ids: ["dev"],
  fix_route: "/integrations/1",
  provider: "github",
  can_fix: true,
  ...over,
});

const manual = (over: Partial<FleetLivePrerequisite>): FleetLivePrerequisite =>
  line({
    check: "manual:legal-sign-off",
    kind: "manual",
    label: "Legal sign-off",
    state: "to_confirm",
    member_ids: [],
    fix_route: "",
    provider: "",
    can_fix: false,
    ...over,
  });

const names: Record<string, string> = { dev: "Developer", ux: "UX Director" };
const memberName = (id: string) => names[id] ?? "";
const t = (id: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((s, [k, v]) => s.replace(`{${k}}`, String(v)), id);
const iso = (d: string) => d;

function renderList(lines: FleetLivePrerequisite[], readOnly = false) {
  return renderWithProviders(
    <MemoryRouter>
      <FleetAboutPrerequisites fleetId="f1" lines={lines} memberName={memberName} readOnly={readOnly} />
    </MemoryRouter>,
  );
}

describe("FleetAboutPrerequisites", () => {
  it("a complete fleet folds every line into N met, with no Fix and no Fix with AI; the toggle shows them", () => {
    renderList([
      line({ member_ids: ["dev", "ux"] }),
      line({ check: "agent_account:a", kind: "agent_account", label: "Outlook mailbox", provider: "", member_ids: ["dev"] }),
    ]);
    expect(screen.queryAllByTestId("about-prerequisite")).toHaveLength(0);
    const toggle = screen.getByTestId("about-prerequisites-met-toggle");
    expect(toggle).toHaveTextContent("2 met");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const rows = screen.getAllByTestId("about-prerequisite");
    expect(rows.map((r) => r.dataset.state)).toEqual(["met", "met"]);
    expect(within(rows[0]).getByText("Needed by Developer, UX Director")).toBeTruthy();
    // A line without a provider logo wears its kind's icon in the same slot.
    expect(within(rows[1]).getByTestId("about-prerequisite-kind")).toBeTruthy();
    expect(screen.queryByTestId("about-prerequisite-fix")).toBeNull();
    expect(screen.queryByTestId("prerequisites-fix-with-ai")).toBeNull();
    expect(screen.queryByTestId("about-prerequisite-confirm")).toBeNull();
  });

  it("a broken line comes first, says why from its reason code and links to its fix", () => {
    renderList([
      line({}),
      line({ check: "binding:2", label: "Jira", state: "not_met", reason: "raw", reason_code: "integration_error", fix_route: "/integrations/2" }),
    ]);
    const rows = screen.getAllByTestId("about-prerequisite");
    expect(rows[0].dataset.state).toBe("not_met");
    expect(within(rows[0]).getByText("In error: reconnect it")).toBeTruthy();
    expect(within(rows[0]).queryByText("raw")).toBeNull();
    expect(within(rows[0]).getByRole("link", { name: "Fix it" }).getAttribute("href")).toBe("/integrations/2");
    expect(screen.getByRole("button", { name: "Fix with AI" })).toBeTruthy();
  });

  it("a fix the reader may not open says only an administrator can, with no Fix and no Fix with AI", () => {
    renderList([
      line({
        check: "profile:sector",
        kind: "profile",
        provider: "",
        label: "Sector in the organization profile",
        label_key: "Sector in the organization profile",
        state: "not_met",
        reason_code: "not_filled",
        fix_route: "/settings/organization?card=identity",
        can_fix: false,
      }),
    ]);
    const row = screen.getByTestId("about-prerequisite");
    expect(within(row).getByText("Only an administrator can fix this")).toBeTruthy();
    expect(screen.queryByTestId("about-prerequisite-fix")).toBeNull();
    expect(screen.queryByTestId("prerequisites-fix-with-ai")).toBeNull();
  });

  it("on a read-only fleet offers no Fix it, no Fix with AI and no Confirm, and states the lock (UX r2 B1 on #464)", () => {
    renderList(
      [
        line({ check: "binding:2", label: "Jira", state: "not_met", reason_code: "integration_error", fix_route: "/integrations/2" }),
        manual({ can_confirm: true }),
      ],
      true,
    );
    expect(screen.queryByTestId("about-prerequisite-fix")).toBeNull();
    expect(screen.queryByTestId("prerequisites-fix-with-ai")).toBeNull();
    expect(screen.queryByRole("button", { name: "Fix with AI" })).toBeNull();
    expect(screen.queryByTestId("about-prerequisite-confirm")).toBeNull();
    expect(screen.queryByTestId("about-prerequisite-admin-only")).toBeNull();
    expect(screen.getByTestId("about-prerequisite-read-only").textContent).toBe("Platform Health is built in and read-only.");
  });

  it("Platform Health off on its read-only fleet links to Settings, without the read-only line (UX r1 B3 on #980)", () => {
    renderList(
      [
        line({
          check: "automations:enabled",
          kind: "automations",
          provider: "",
          label: "Platform Health switched on in Settings",
          label_key: "Platform Health switched on in Settings",
          state: "not_met",
          reason_code: "platform_health_off",
          fix_route: "/settings/automations/health",
          can_fix: true,
        }),
      ],
      true,
    );
    const row = screen.getByTestId("about-prerequisite");
    expect(within(row).getByText("Platform Health is off in Settings")).toBeTruthy();
    expect(within(row).getByRole("link", { name: "Fix it" }).getAttribute("href")).toBe("/settings/automations/health");
    expect(screen.queryByTestId("about-prerequisite-read-only")).toBeNull();
    expect(screen.queryByRole("button", { name: "Fix with AI" })).toBeNull();
  });

  it("a line the reader may not see says so, before a manual line to confirm", () => {
    renderList([
      manual({ can_confirm: true }),
      line({ state: "unreadable", reason_code: "not_shared" }),
    ]);
    expect(screen.getAllByTestId("about-prerequisite").map((r) => r.dataset.state)).toEqual(["unreadable", "to_confirm"]);
  });

  it("a manual line to confirm offers Confirm and Fix with AI to a fleet manager; Confirm stores it", () => {
    mutate.mockClear();
    ask.mockClear();
    renderList([manual({ can_confirm: true })]);
    const row = screen.getByTestId("about-prerequisite");
    expect(within(row).getByText("Legal sign-off")).toBeTruthy();
    expect(within(row).getByText("A person confirms this: no system can read it")).toBeTruthy();
    fireEvent.click(within(row).getByTestId("about-prerequisite-confirm"));
    expect(mutate).toHaveBeenCalledWith({ check: "manual:legal-sign-off", confirmed: true });
    fireEvent.click(within(row).getByTestId("about-prerequisite-confirm-with-ai"));
    expect(ask).toHaveBeenCalledTimes(1);
    expect(String(ask.mock.calls[0][0])).toContain("manual:legal-sign-off");
  });

  it("a confirmed manual line folds with the met lines, says who and when, and Undo unconfirms it", () => {
    mutate.mockClear();
    renderList([manual({ can_confirm: true, state: "met", confirmed_by: "Ada Lovelace", confirmed_at: "2026-11-09T10:00:00+00:00" })]);
    expect(screen.queryAllByTestId("about-prerequisite")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("about-prerequisites-met-toggle"));
    const row = screen.getByTestId("about-prerequisite");
    expect(within(row).getByText(/Confirmed by Ada Lovelace on/)).toBeTruthy();
    fireEvent.click(within(row).getByTestId("about-prerequisite-undo"));
    expect(mutate).toHaveBeenCalledWith({ check: "manual:legal-sign-off", confirmed: false });
  });

  it("a reader who does not manage the fleet is told who confirms it, with no button", () => {
    renderList([manual({ can_confirm: false })]);
    const row = screen.getByTestId("about-prerequisite");
    expect(within(row).getByText("A fleet manager confirms this: no system can read it")).toBeTruthy();
    expect(within(row).queryByRole("button")).toBeNull();
  });

  it("an expired confirmation is back to confirm and says so", () => {
    renderList([manual({ can_confirm: true, confirmed_by: "Ada", confirmed_at: "2026-01-01T00:00:00+00:00" })]);
    const row = screen.getByTestId("about-prerequisite");
    expect(within(row).getByText(/over 90 days ago: confirm it again/)).toBeTruthy();
    expect(within(row).getByTestId("about-prerequisite-confirm")).toBeTruthy();
  });

  it("never renders a line to confirm without a Confirm action or the sentence that names who confirms it", () => {
    for (const can_confirm of [true, false]) {
      const { unmount } = renderList([manual({ can_confirm })]);
      const row = screen.getByTestId("about-prerequisite");
      const action = within(row).queryByTestId("about-prerequisite-confirm");
      const told = within(row).queryByText("A fleet manager confirms this: no system can read it");
      expect(Boolean(action) || Boolean(told)).toBe(true);
      unmount();
    }
  });
});

describe("FleetPrerequisitesChip", () => {
  it("reads All prerequisites met when every line is met, a confirmed manual line included", () => {
    renderWithProviders(
      <MemoryRouter>
        <FleetPrerequisitesChip fleetId="f1" lines={[line({}), manual({ state: "met", confirmed_by: "Ada", confirmed_at: "2026-11-09" })]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("about-prerequisites-chip").textContent).toBe("All prerequisites met");
  });

  it("says 1 to confirm with Confirm while a manual line waits, never All prerequisites met", () => {
    mutate.mockClear();
    renderWithProviders(
      <MemoryRouter>
        <FleetPrerequisitesChip fleetId="f1" lines={[line({}), manual({ can_confirm: true })]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("about-prerequisites-chip").textContent).toBe("Prerequisites 1/2");
    expect(screen.getByTestId("about-prerequisites-to-confirm").textContent).toBe("1 to confirm");
    fireEvent.click(screen.getByTestId("prerequisites-chip-confirm"));
    expect(mutate).toHaveBeenCalledWith({ check: "manual:legal-sign-off", confirmed: true });
  });

  it("counts met over checked and offers Fix with AI while a fixable line fails", () => {
    renderWithProviders(
      <MemoryRouter>
        <FleetPrerequisitesChip fleetId="f1" lines={[line({}), line({ check: "b", state: "not_met", reason_code: "deleted" })]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("about-prerequisites-chip").textContent).toBe("Prerequisites 1/2");
    expect(screen.getByRole("button", { name: "Fix with AI" })).toBeTruthy();
  });

  it("on a read-only fleet keeps the counts and offers no Confirm and no Fix with AI (UX r1 B3 on #464)", () => {
    renderWithProviders(
      <MemoryRouter>
        <FleetPrerequisitesChip
          fleetId="f1"
          readOnly
          lines={[line({ check: "b", state: "not_met", reason_code: "deleted" }), manual({ can_confirm: true })]}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("about-prerequisites-chip").textContent).toBe("Prerequisites 0/2");
    expect(screen.queryByRole("button", { name: "Fix with AI" })).toBeNull();
    expect(screen.queryByTestId("prerequisites-chip-confirm")).toBeNull();
  });
});

describe("prerequisite helpers", () => {
  it("count the checked lines and list the fixable failing ones with their translated reason and check", () => {
    const lines = [
      line({}),
      line({ check: "b", label: "Jira", state: "not_met", reason_code: "token_expired", reason_params: { date: "2026-11-03" } }),
      line({ check: "c", label: "Slack", state: "not_met", reason_code: "deleted", can_fix: false }),
    ];
    const summary = prerequisiteSummary(lines);
    expect([summary.met, summary.total, summary.failing.length]).toEqual([1, 3, 2]);
    expect(failingLinesText(summary.fixable, t, iso)).toBe("- Jira: Token expired on 2026-11-03 (b)");
  });

  it("translate a platform label, keep a name, and never show an unknown code or a raw status", () => {
    expect(prerequisiteLabel(line({ label: "x", label_key: "Handover to {name}", label_params: { name: "Legal" } }), t)).toBe(
      "Handover to Legal",
    );
    expect(prerequisiteLabel(line({ label: "My KB", label_key: "" }), t)).toBe("My KB");
    expect(prerequisiteReason(line({ reason: "configured: reconnect it", reason_code: "nope" }), t, iso)).toBe("");
    expect(prerequisiteReason(line({ reason_code: "account_paused", reason_params: { detail: "token revoked" } }), t, iso)).toBe(
      "Paused: token revoked",
    );
  });

  it("every reason and label has its translation in the four catalogs", () => {
    const keys = [
      ...Object.values(PREREQUISITE_REASONS),
      ...PREREQUISITE_LABELS,
      "Only an administrator can fix this",
      "Confirmed by {name} on {date}",
      "A person confirms this: no system can read it",
      "A fleet manager confirms this: no system can read it",
    ];
    for (const catalog of [en, fr, es, ja] as Record<string, string>[]) {
      for (const key of keys) expect(catalog[key], key).toBeTruthy();
    }
    for (const catalog of [fr, es, ja] as Record<string, string>[]) {
      expect(catalog["In error: reconnect it"]).not.toBe("In error: reconnect it");
    }
  });
});
