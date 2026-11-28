// #2528: the purge of every signal is an administrators-only icon button; its dialog shows the live
// counts, keeps the resync off by default, and starts only once the shown code is typed back.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { SignalsPurgeAction } from "./SignalsPurgeAction";

const state = vi.hoisted(() => ({
  isAdmin: true,
  started: [] as unknown[],
  info: { counts: { signals: 4400, cases: 37 }, confirm_word: "PURGE", job: null } as Record<string, unknown>,
}));

vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: state.isAdmin }),
}));

vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
  useCtemSignalPurge: () => ({ data: state.info, isError: false, error: null, refetch: () => Promise.resolve() }),
  useStartCtemSignalPurge: () => ({
    isPending: false,
    mutate: (body: unknown, opts?: { onSuccess?: () => void }) => {
      state.started.push(body);
      opts?.onSuccess?.();
    },
  }),
}));

describe("SignalsPurgeAction", () => {
  beforeEach(() => {
    state.isAdmin = true;
    state.started = [];
    state.info = {
      counts: { signals: 4400, cases: 37 },
      confirm_word: "PURGE",
      resync_routes: [
        { key: "vulnerability", group: "world", label: "Vulnerabilities", default: 7, windowed: true },
        { key: "intrusion_set", group: "world", label: "Intrusion sets", default: 30, windowed: true },
        { key: "aikido", group: "inside", label: "Aikido open issues", default: null, windowed: true },
        { key: "gitlab", group: "inside", label: "GitLab dependency findings", default: null, windowed: false, note: "Recorded by the GitLab asset sync" },
        { key: "attack_surface", group: "pass", label: "Attack surface pass", default: null, windowed: false },
      ],
      resync_max_days: 365,
      job: null,
    };
  });

  it("is not drawn for a non-administrator", () => {
    state.isAdmin = false;
    renderWithProviders(<SignalsPurgeAction />);
    expect(screen.queryByTestId("ctem-signals-purge")).toBeNull();
  });

  it("shows the live counts and keeps the resync unchecked by default", () => {
    renderWithProviders(<SignalsPurgeAction />);
    const trigger = screen.getByTestId("ctem-signals-purge");
    expect(trigger.getAttribute("aria-label")).toBe("Purge all signals");
    fireEvent.click(trigger);
    const preview = screen.getByTestId("ctem-signals-purge-preview");
    expect(preview.textContent).toContain("4,400 signals");
    expect(preview.textContent).toContain("37 cases");
    expect(screen.getByRole("checkbox", { name: "Resync signals from sources after the purge" }).getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("button", { name: "Purge" })).toHaveProperty("disabled", true);
  });

  // #2578: the resync shows one window per route, its default filled in, and sends what is chosen.
  it("starts the purge with the resync and the chosen windows once the code is typed back", () => {
    renderWithProviders(<SignalsPurgeAction />);
    fireEvent.click(screen.getByTestId("ctem-signals-purge"));
    expect(screen.queryByTestId("ctem-signals-purge-windows")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "Resync signals from sources after the purge" }));
    const vulns = screen.getByRole("textbox", { name: "How far back Vulnerabilities is read" }) as HTMLInputElement;
    expect(vulns.value).toBe("7");
    expect((screen.getByRole("textbox", { name: "How far back Aikido open issues is read" }) as HTMLInputElement).value).toBe("");
    expect(screen.getByTestId("ctem-signals-purge-window-attack_surface").textContent).toContain("Runs once");
    // Every source and pass starts ticked (#2745); an info row has no box.
    expect(screen.getByRole("checkbox", { name: "Vulnerabilities" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("checkbox", { name: "Attack surface pass" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.queryByRole("checkbox", { name: "GitLab dependency findings" })).toBeNull();
    // A route the resync does not read is a read-only line with its reason, never a field (UX round 1 of #2590, B3).
    expect(screen.queryByRole("textbox", { name: "GitLab dependency findings" })).toBeNull();
    expect(screen.getByTestId("ctem-signals-purge-window-gitlab").textContent).toContain("Recorded by the GitLab asset sync");
    fireEvent.change(vulns, { target: { value: "3" } });
    const code = screen.getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(screen.getByRole("textbox", { name: "To confirm, type the code below:" }), { target: { value: code } });
    fireEvent.click(screen.getByRole("button", { name: "Purge" }));
    expect(state.started).toEqual([
      {
        confirm: "PURGE",
        resync: true,
        resync_sources: {
          vulnerability: { enabled: true, window: 3, unit: "days" },
          intrusion_set: { enabled: true, window: 30, unit: "days" },
          aikido: { enabled: true, window: null, unit: "days" },
          attack_surface: { enabled: true },
        },
      },
    ]);
  });

  it("leaves an unticked source and pass out of the resync, its field disabled (#2745)", () => {
    renderWithProviders(<SignalsPurgeAction />);
    fireEvent.click(screen.getByTestId("ctem-signals-purge"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Resync signals from sources after the purge" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Vulnerabilities" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Attack surface pass" }));
    const vulns = screen.getByRole("textbox", { name: "How far back Vulnerabilities is read" });
    expect(vulns).toHaveProperty("disabled", true);
    expect(screen.getByTestId("ctem-signals-purge-window-attack_surface").textContent).toContain("Does not run");
    // An empty field on an unticked source blocks nothing.
    fireEvent.change(vulns, { target: { value: "" } });
    const code = screen.getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(screen.getByRole("textbox", { name: "To confirm, type the code below:" }), { target: { value: code } });
    fireEvent.click(screen.getByRole("button", { name: "Purge" }));
    expect(state.started[state.started.length - 1]).toEqual({
      confirm: "PURGE",
      resync: true,
      resync_sources: {
        vulnerability: { enabled: false },
        intrusion_set: { enabled: true, window: 30, unit: "days" },
        aikido: { enabled: true, window: null, unit: "days" },
        attack_surface: { enabled: false },
      },
    });
  });

  it("keeps the purge disabled while a window is out of range", () => {
    renderWithProviders(<SignalsPurgeAction />);
    fireEvent.click(screen.getByTestId("ctem-signals-purge"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Resync signals from sources after the purge" }));
    fireEvent.change(screen.getByRole("textbox", { name: "How far back Vulnerabilities is read" }), { target: { value: "0" } });
    const code = screen.getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(screen.getByRole("textbox", { name: "To confirm, type the code below:" }), { target: { value: code } });
    // The message sits under the invalid field itself, visible and tied to it (UX round 1 of #2590, B2).
    const field = screen.getByRole("textbox", { name: "How far back Vulnerabilities is read" });
    const error = screen.getByRole("alert");
    expect(error.textContent).toContain("Enter a whole number from 1 to 365");
    expect(field.getAttribute("aria-describedby")).toBe(error.id);
    // One message, under the field: no second copy hidden at the bottom of the list.
    expect(screen.getAllByText("Enter a whole number from 1 to 365")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Purge" })).toHaveProperty("disabled", true);
  });

  it("lists what the resync read and recorded on each route once the job ends", () => {
    const done = { state: "done", counts: {} };
    state.info = {
      ...state.info,
      job: {
        id: "j1",
        state: "done",
        resync: true,
        step: "resume",
        steps: {
          pause: done,
          purge: done,
          resync: {
            state: "done",
            counts: {
              routes: {
                vulnerability: { window_days: 7, read: 5000, recorded: 40, capped: true },
                aikido: { window_days: null, read: 0, recorded: 0, skipped: "no Aikido integration" },
                intrusion_set: { window_days: 30, read: 2, recorded: 0, errors: ["OpenCTI: timeout"] },
              },
            },
          },
          resume: done,
        },
        error: null,
        started_at: "2026-11-27T08:00:00Z",
        finished_at: "2026-11-27T08:10:00Z",
      },
    };
    renderWithProviders(<SignalsPurgeAction />);
    fireEvent.click(screen.getByTestId("ctem-signals-purge"));
    const code = screen.getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(screen.getByRole("textbox", { name: "To confirm, type the code below:" }), { target: { value: code } });
    fireEvent.click(screen.getByRole("button", { name: "Purge" }));
    const result = screen.getByTestId("ctem-signals-purge-result");
    expect(screen.getByTestId("ctem-signals-purge-result-vulnerability").textContent).toContain("5000 read, 40 recorded");
    expect(screen.getByTestId("ctem-signals-purge-result-vulnerability").textContent).toContain("Stopped at the read limit");
    expect(screen.getByTestId("ctem-signals-purge-result-aikido").getAttribute("data-kind")).toBe("skipped");
    expect(screen.getByTestId("ctem-signals-purge-result-aikido").textContent).toContain("no Aikido integration");
    expect(screen.getByTestId("ctem-signals-purge-result-intrusion_set").getAttribute("data-kind")).toBe("error");
    expect(result.textContent).toContain("Intrusion sets");
  });

  // #2539: a job whose process restarted is read back interrupted (failed): a new purge can start.
  it("offers a new purge after an interrupted job and says why it stopped", () => {
    const steps = { state: "done", counts: {} };
    state.info = {
      ...state.info,
      job: {
        id: "j9",
        state: "failed",
        interrupted: true,
        intake_paused: false,
        resync: false,
        step: "purge",
        steps: { pause: steps, purge: { state: "failed", counts: { cases: 12 } }, resync: { state: "skipped", counts: {} }, resume: { state: "done", counts: {} } },
        error: "interrupted",
        started_at: "2026-11-26T17:00:00Z",
        finished_at: null,
      },
    };
    renderWithProviders(<SignalsPurgeAction />);
    fireEvent.click(screen.getByTestId("ctem-signals-purge"));
    expect(screen.getByTestId("ctem-signals-purge-preview")).toBeTruthy();
    expect(screen.queryByTestId("ctem-signals-purge-progress")).toBeNull();
    const code = screen.getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(screen.getByRole("textbox"), { target: { value: code } });
    fireEvent.click(screen.getByRole("button", { name: "Purge" }));
    expect(state.started).toEqual([{ confirm: "PURGE", resync: false }]);
    expect(screen.getByText("Purge interrupted")).toBeTruthy();
    // UX B1: the pause expired, so the dialog says intake runs again and the resume step is done.
    expect(screen.getByTestId("ctem-signals-purge-progress").textContent).toContain("Signal intake is running again.");
    expect(screen.getByTestId("ctem-signals-purge-step-resume").getAttribute("data-state")).toBe("done");
  });

  // #2539 UX B1: the pause key outlived the job lock: the dialog says intake is still paused.
  it("says intake is still paused when an interrupted job left its pause", () => {
    state.info = {
      ...state.info,
      job: {
        id: "j12",
        state: "failed",
        interrupted: true,
        intake_paused: true,
        resync: true,
        step: "purge",
        steps: {
          pause: { state: "done", counts: {} },
          purge: { state: "failed", counts: {} },
          resync: { state: "pending", counts: {} },
          resume: { state: "pending", counts: {} },
        },
        error: "interrupted",
        started_at: "2026-11-26T17:00:00Z",
        finished_at: null,
      },
    };
    renderWithProviders(<SignalsPurgeAction />);
    fireEvent.click(screen.getByTestId("ctem-signals-purge"));
    const code = screen.getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(screen.getByRole("textbox"), { target: { value: code } });
    fireEvent.click(screen.getByRole("button", { name: "Purge" }));
    const progress = screen.getByTestId("ctem-signals-purge-progress").textContent ?? "";
    expect(progress).toContain("Signal intake is still paused.");
    expect(progress).not.toContain("Signal intake is running again.");
    expect(screen.getByTestId("ctem-signals-purge-step-resume").getAttribute("data-state")).toBe("pending");
  });
});
