// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { CtemPlatformSource } from "@/hooks/api/useCtemSignalsApi";
import { PlatformSourceControls } from "./PlatformSourceControls";
import { holdsLegacyCleanup, sharedFeeds } from "./platform-sources";

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

describe("Remove legacy OpenCTI objects (#2583 point 4)", () => {
  it("shows on the OpenCTI live listener once a source is on, for an administrator only", () => {
    expect(holdsLegacyCleanup({ provider: "opencti", kind: "listener" }, [{ enabled: true }])).toBe(true);
    expect(holdsLegacyCleanup({ provider: "opencti", kind: "listener" }, [{ enabled: false }])).toBe(false);
    expect(holdsLegacyCleanup({ provider: "opencti", kind: "pir_playbook" }, [{ enabled: true }])).toBe(false);
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener()]} isAdmin={false} onEnable={() => {}} />);
    expect(screen.queryByTestId("ctem-legacy-cleanup")).toBeNull();
  });

  it("lists first with a dry run, then removes on confirm", async () => {
    const objects = [{ kind: "triggersKnowledge", id: "x1", name: "XTM One - Threat response - New relevant threats" }];
    post.mockResolvedValueOnce({ dry_run: true, integrations: [{ integration_id: "i1", objects, removed: 0, errors: [] }] });
    post.mockResolvedValueOnce({ dry_run: false, integrations: [{ integration_id: "i1", objects, removed: 1, errors: [] }] });
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener()]} isAdmin onEnable={() => {}} />);
    fireEvent.click(screen.getByTestId("ctem-legacy-cleanup"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ctem-signals/sources/legacy-opencti-objects?dry_run=true", {}));
    const dialog = await screen.findByTestId("ctem-legacy-cleanup-dialog");
    expect(await within(dialog).findByText("1 object would be removed:")).toBeInTheDocument();
    expect(within(dialog).getByText("OpenCTI prod")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByTestId("ctem-legacy-cleanup-confirm"));
    await waitFor(() => expect(post).toHaveBeenLastCalledWith("/ctem-signals/sources/legacy-opencti-objects?dry_run=false", {}));
    expect(await within(dialog).findByText("1 object removed.")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("ctem-legacy-cleanup-confirm")).toBeNull();
  });

  it("cannot remove when nothing is left", async () => {
    post.mockResolvedValueOnce({ dry_run: true, integrations: [{ integration_id: "i1", objects: [], removed: 0, errors: [] }] });
    renderWithProviders(<PlatformSourceControls source={{ provider: "opencti", kind: "listener" }} platformSources={[listener()]} isAdmin onEnable={() => {}} />);
    fireEvent.click(screen.getByTestId("ctem-legacy-cleanup"));
    const dialog = await screen.findByTestId("ctem-legacy-cleanup-dialog");
    expect(await within(dialog).findByText("No legacy object left.")).toBeInTheDocument();
    expect(within(dialog).getByTestId("ctem-legacy-cleanup-confirm")).toBeDisabled();
  });
});
