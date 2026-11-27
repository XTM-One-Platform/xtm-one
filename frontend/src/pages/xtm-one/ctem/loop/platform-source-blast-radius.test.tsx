// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { CtemPlatformSource } from "@/hooks/api/useCtemSignalsApi";
import { PlatformSourceControls } from "./PlatformSourceControls";
import { legacyCleanupCount, moduleEventText, sharedFeeds } from "./platform-sources";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const post = vi.mocked(api.post);
const get = vi.mocked(api.get);

function listener(over: Partial<CtemPlatformSource> = {}): CtemPlatformSource {
  return {
    id: "s1",
    provider: "opencti",
    kind: "listener",
    integration_id: "i1",
    integration_name: "OpenCTI prod",
    integration_connected: true,
    enabled: true,
    run_as_user_id: null,
    status: "connected",
    reason: null,
    reason_code: null,
    reason_params: {},
    checked_at: null,
    label: "OpenCTI",
    objects: [
      { kind: "notifier", id: "n1", name: "XTM One - Signals - OpenCTI live listener - Notifier" },
      { kind: "trigger", id: "t1", name: "XTM One - Signals - OpenCTI live listener - Trigger" },
    ],
    integration_url: "https://octi.example.com",
    feeds: [
      { path: "new_vulnerability", label: "New vulnerability on a running version" },
      { path: "relevant_threat", label: "Relevant new threat or actor" },
      { path: "credential_leak", label: "Credential or secret leak" },
    ],
    ...over,
  } as CtemPlatformSource;
}

beforeEach(() => {
  post.mockReset();
  get.mockReset();
  get.mockResolvedValue([]);
});

describe("Blast radius of a shared source (#2583 point 6)", () => {
  it("names every path the one switch stops, and the objects it holds by name", () => {
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener()]} isAdmin onEnable={() => {}} />);
    const radius = screen.getByTestId("ctem-platform-source-blast-radius");
    expect(within(radius).getByTestId("ctem-platform-source-feeds").textContent).toBe(
      "Feeds 3 paths: New vulnerability on a running version, Relevant new threat or actor, Credential or secret leak. Turning it off stops all of them.",
    );
    expect(within(radius).getByText("XTM One - Signals - OpenCTI live listener - Trigger")).toHaveAttribute(
      "title",
      "XTM One - Signals - OpenCTI live listener - Trigger",
    );
  });

  it("says nothing more for a source that feeds one path", () => {
    expect(sharedFeeds({ feeds: [{ path: "a", label: "A" }] })).toEqual([]);
    expect(sharedFeeds({ feeds: undefined })).toEqual([]);
  });
});

describe("Owner of each provisioned object (#2640)", () => {
  it("counts only the source's own objects and lists the fleets' objects apart, each with its owner", () => {
    const source = listener({
      provider: "openaev",
      objects: [
        { kind: "notifier", id: "n1", name: "XTM One - Signals - OpenAEV listener - Notifier", owner: "Signals" },
        {
          kind: "trigger",
          id: "t1",
          name: "XTM One - Signals - OpenAEV listener - Trigger",
          owner: "Signals",
          event: "simulation_finished",
          shared_with: ["Detection validation desk"],
        },
      ],
      fleet_objects: [
        {
          kind: "trigger",
          id: "t9",
          name: "XTM One - Detection validation desk - Finished simulations - Trigger",
          owner: "Detection validation desk",
          event: "simulation_finished",
        },
      ],
    });
    renderWithProviders(<PlatformSourceControls source={{ provider: "openaev", kind: "listener" }} platformSources={[source]} isAdmin onEnable={() => {}} />);
    const radius = screen.getByTestId("ctem-platform-source-blast-radius");
    expect(within(radius).getByText("Objects in OpenAEV, owned by Signals (2 objects):")).toBeInTheDocument();
    const own = within(radius).getByTestId("ctem-platform-source-object-names");
    expect(within(own).getAllByTestId("ctem-platform-source-object")).toHaveLength(2);
    expect(within(own).getAllByText("Owner: Signals")).toHaveLength(2);
    expect(within(own).getByTestId("ctem-platform-source-object-shared").textContent).toBe(
      "Detection validation desk also listens to the \"Simulation finished\" event with its own trigger: each owner gets its own delivery and starts its own run, as its own run-as user.",
    );
    expect(within(own).getByTestId("ctem-platform-source-object-shared").textContent).not.toContain("simulation_finished");
    const fleets = within(radius).getByTestId("ctem-platform-source-fleet-objects");
    expect(within(fleets).getByText("Owner: Detection validation desk")).toBeInTheDocument();
    expect(within(radius).getByText("Other XTM One objects in OpenAEV, owned by fleets, not counted here (1 object):")).toBeInTheDocument();
  });
});

describe("Module event in words (UX round 1 on #2664, B1)", () => {
  it("names a known event by its label and falls back to the code only for an unknown one", () => {
    const t = (key: string) => `t:${key}`;
    expect(moduleEventText(t, "simulation_finished")).toBe("t:Simulation finished");
    expect(moduleEventText(t, "finding_updated")).toBe("t:Finding updated");
    expect(moduleEventText(t, "some_new_event")).toBe("some_new_event");
  });
});

describe("Several other owners of one event (UX round 2 on #2664, B1)", () => {
  it("joins the owners in words and agrees the verb with their number", () => {
    const source = listener({
      provider: "openaev",
      objects: [
        {
          kind: "trigger",
          id: "t1",
          name: "XTM One - Signals - OpenAEV listener - Trigger",
          owner: "Signals",
          event: "simulation_finished",
          shared_with: ["CTEM Orchestrator", "Exposure desk"],
        },
      ],
    });
    renderWithProviders(<PlatformSourceControls source={{ provider: "openaev", kind: "listener" }} platformSources={[source]} isAdmin onEnable={() => {}} />);
    const line = screen.getByTestId("ctem-platform-source-object-shared").textContent ?? "";
    expect(line).toBe(
      "CTEM Orchestrator and Exposure desk also listen to the \"Simulation finished\" event with its own trigger: each owner gets its own delivery and starts its own run, as its own run-as user.",
    );
    expect(line).not.toContain("CTEM Orchestrator, Exposure desk");
  });
});

describe("Remove legacy OpenCTI objects (#2583 point 4, #2627)", () => {
  it("counts what XTM One still holds on the OpenCTI live listener, once a source is on", () => {
    expect(legacyCleanupCount({ provider: "opencti", kind: "listener" }, [{ enabled: true, legacy_pending: 3 }, { enabled: true, legacy_pending: 1 }])).toBe(4);
    expect(legacyCleanupCount({ provider: "opencti", kind: "listener" }, [{ enabled: false, legacy_pending: 3 }])).toBe(0);
    expect(legacyCleanupCount({ provider: "opencti", kind: "pir_playbook" }, [{ enabled: true, legacy_pending: 3 }])).toBe(0);
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener({ legacy_pending: 2 })]} isAdmin={false} onEnable={() => {}} />);
    expect(screen.queryByTestId("ctem-legacy-cleanup")).toBeNull();
  });

  it("hides the action when nothing is pending", () => {
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener({ legacy_pending: 0 })]} isAdmin onEnable={() => {}} />);
    expect(screen.queryByTestId("ctem-legacy-cleanup")).toBeNull();
  });

  it("shows the action with its count when objects are pending", () => {
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener({ legacy_pending: 3 })]} isAdmin onEnable={() => {}} />);
    expect(screen.getByTestId("ctem-legacy-cleanup")).toHaveTextContent("Remove legacy OpenCTI objects (3)");
  });

  it("lists first with a dry run, then removes on confirm", async () => {
    const objects = [{ kind: "triggersKnowledge", id: "x1", name: "XTM One - Threat response - New relevant threats" }];
    post.mockResolvedValueOnce({ dry_run: true, integrations: [{ integration_id: "i1", objects, removed: 0, errors: [] }] });
    post.mockResolvedValueOnce({ dry_run: false, integrations: [{ integration_id: "i1", objects, removed: 1, errors: [] }] });
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener({ legacy_pending: 1 })]} isAdmin onEnable={() => {}} />);
    fireEvent.click(screen.getByTestId("ctem-legacy-cleanup"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ctem-signals/sources/legacy-opencti-objects?dry_run=true", {}));
    const dialog = await screen.findByTestId("ctem-legacy-cleanup-dialog");
    expect(await within(dialog).findByText("1 object would be removed:")).toBeInTheDocument();
    expect(within(dialog).getByTestId("ctem-legacy-cleanup-confirm")).toHaveTextContent("Remove 1 object");
    expect(within(dialog).getByText("OpenCTI prod")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByTestId("ctem-legacy-cleanup-confirm"));
    await waitFor(() => expect(post).toHaveBeenLastCalledWith("/ctem-signals/sources/legacy-opencti-objects?dry_run=false", {}));
    expect(await within(dialog).findByText("1 object removed.")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("ctem-legacy-cleanup-confirm")).toBeNull();
  });

  it("offers only Close when the dry run finds nothing left", async () => {
    post.mockResolvedValueOnce({ dry_run: true, integrations: [{ integration_id: "i1", objects: [], removed: 0, errors: [] }] });
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener({ legacy_pending: 2 })]} isAdmin onEnable={() => {}} />);
    fireEvent.click(screen.getByTestId("ctem-legacy-cleanup"));
    const dialog = await screen.findByTestId("ctem-legacy-cleanup-dialog");
    expect(await within(dialog).findByText("No legacy object left.")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("ctem-legacy-cleanup-confirm")).toBeNull();
    expect(within(dialog).getByText("Close")).toBeInTheDocument();
  });
});

describe("Open in links of a per-path source (#2627)", () => {
  it("labels two playbooks of one row with the path each serves", () => {
    const pir = listener({
      kind: "pir_playbook",
      objects: [
        { kind: "notifier", id: "n1" },
        { kind: "playbook", id: "p1", path: "new_vulnerability", path_label: "New vulnerability on a running version" },
        { kind: "playbook", id: "p2", path: "relevant_threat", path_label: "Relevant new threat or actor" },
      ],
    });
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "pir_playbook" }} platformSources={[pir]} isAdmin onEnable={() => {}} />);
    const texts = screen.getAllByTestId("ctem-platform-source-object-link").map((a) => a.textContent);
    expect(texts).toEqual(["Notifier", "Playbook: New vulnerability on a running version", "Playbook: Relevant new threat or actor"]);
    // #2649 UX r1 B1: the links take their own line under the name and switch, not the first line beside them.
    const objects = screen.getByTestId("ctem-platform-source-objects");
    const row = screen.getByTestId("ctem-platform-source-switch").closest("li");
    expect(objects.parentElement).toBe(row);
    expect(screen.getByTestId("ctem-platform-source-switch").parentElement).not.toContainElement(objects);
    expect(screen.getByTestId("ctem-platform-source-name")).toHaveTextContent(pir.integration_name);
  });
});
