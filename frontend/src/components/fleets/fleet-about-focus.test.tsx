/**
 * The fleet About view keeps the keyboard focus (#9768 S2, UX round 1 B2) and
 * offers the version history (B1) - `yarn test:unit` (vitest + happy-dom).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import type { FleetDocumentation } from "@/hooks/api/useFleetApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { FleetAboutView } from "./FleetAboutView";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), download: vi.fn() },
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));
// The showcase draws the sections; this test is about the action row only.
vi.mock("./FleetAboutShowcase", () => ({
  FleetAboutShowcase: () => <p>showcase</p>,
  FleetAboutFlow: () => <p>flow</p>,
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

function doc(fields: Partial<FleetDocumentation>): FleetDocumentation {
  return {
    section_keys: ["purpose"],
    sections: { purpose: "Why it exists" },
    version: 2,
    updated_at: "2026-11-07T17:00:00Z",
    updated_by: "7c1f0000-0000-4000-8000-000000000000",
    editable: true,
    local: true,
    out_of_date: false,
    ...fields,
  };
}

/** Serves `first`, then what the last write answered, as the server would. */
function serve(first: FleetDocumentation) {
  let current = first;
  post.mockImplementation(async () => {
    current = next ?? current;
    return current;
  });
  get.mockImplementation(async (url: string) => {
    if (url.endsWith("/documentation")) return current;
    if (url.endsWith("/documentation/history")) {
      return {
        versions: [
          { version: 2, updated_at: current.updated_at, updated_by: current.updated_by, section_keys: ["purpose"], current: true },
          { version: 1, updated_at: "2026-11-07T16:00:00Z", updated_by: "seed", section_keys: ["purpose"], current: false },
        ],
      };
    }
    return {};
  });
}

function renderAbout() {
  return renderWithProviders(<FleetAboutView fleetId="f-1" onOpenGraph={() => {}} onOpenMembers={() => {}} />);
}

let next: FleetDocumentation | undefined;

afterEach(() => {
  next = undefined;
  get.mockReset();
  post.mockReset();
  vi.restoreAllMocks();
});

describe("the fleet About actions", () => {
  it("moves the focus to Regenerate once Restore shipped version worked", async () => {
    next = doc({ version: 3, updated_by: "seed", local: false });
    serve(doc({}));
    renderAbout();
    const restore = await screen.findByTestId("about-restore-shipped");
    restore.focus();
    fireEvent.click(restore);
    await waitFor(() => expect(screen.queryByTestId("about-restore-shipped")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("about-regenerate")));
  });

  it("keeps the focus on Regenerate while it runs: aria-disabled, never disabled", async () => {
    serve(doc({}));
    post.mockReturnValue(new Promise(() => {}));
    // A regenerate that never answers: the button stays pending.
    renderAbout();
    const regenerate = await screen.findByTestId("about-regenerate");
    regenerate.focus();
    fireEvent.click(regenerate);
    await waitFor(() => expect(regenerate.getAttribute("aria-disabled")).toBe("true"));
    expect(regenerate.hasAttribute("disabled")).toBe(false);
    expect(document.activeElement).toBe(regenerate);
    fireEvent.click(regenerate);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("lists the kept versions and restores one as the next version", async () => {
    next = doc({ version: 3 });
    serve(doc({}));
    renderAbout();
    fireEvent.click(await screen.findByTestId("about-history"));
    const rows = await screen.findAllByTestId("about-history-version");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("Written by Filigran", { exact: false })).toBeTruthy();
    const restore = screen.getAllByTestId("about-history-restore");
    expect(restore).toHaveLength(1);
    fireEvent.click(restore[0]);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/fleets/f-1/documentation/restore", { version: 1, base_version: 2 }));
  });

  it("moves the focus to the documented layout's Regenerate after the first generation (UX round 2 B2)", async () => {
    next = doc({ version: 1, local: false });
    serve(doc({ version: 0, sections: {}, updated_at: null, updated_by: null, local: false }));
    renderAbout();
    await screen.findByTestId("fleet-about-undocumented");
    const first = screen.getByTestId("about-regenerate");
    first.focus();
    fireEvent.click(first);
    await screen.findByTestId("fleet-about");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("about-regenerate")));
    expect(document.activeElement).not.toBe(document.body);
  });

  it("reads a restored version as a restore, never as 'Generated by AI' (UX round 2 B1)", async () => {
    serve(doc({ version: 3, updated_by: "7c1f0000-0000-4000-8000-000000000000", restored_from: { version: null, author: "seed" } }));
    renderAbout();
    const caption = await screen.findByTestId("about-author");
    expect(caption.textContent).toContain("Restored from the shipped version, written by Filigran");
    expect(caption.textContent).toContain("version 3");
    expect(caption.textContent).not.toContain("Generated by AI");
  });

  it("exports the one-page datasheet from the server, read-only reader included (#9768 S4)", async () => {
    const download = vi.mocked(api.download);
    download.mockResolvedValue({ blob: new Blob(["%PDF"]), filename: "alert-desk-datasheet.pdf" });
    const createObjectURL = vi.fn(() => "blob:x");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    serve(doc({ editable: false, local: false }));
    renderAbout();
    fireEvent.click(await screen.findByTestId("about-export-pdf"));
    await waitFor(() => expect(download).toHaveBeenCalledWith("/fleets/f-1/documentation/datasheet.pdf"));
    expect(post).not.toHaveBeenCalled();
  });

  it("shows the generation started at creation, its one header button spinning and refused (#10316, #10817)", async () => {
    serve(doc({ version: 0, sections: {}, updated_at: null, updated_by: null, local: false, generating: true }));
    renderAbout();
    const panel = await screen.findByTestId("about-generating");
    expect(panel.textContent).toContain("Generating the documentation");
    expect(screen.queryByTestId("about-generate-panel")).toBeNull();
    const buttons = screen.getAllByTestId("about-regenerate");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute("aria-busy")).toBe("true");
    fireEvent.click(buttons[0]);
    expect(post).not.toHaveBeenCalled();
  });

  it("spins on mount for a regenerate running on the server, from another tab or a page left (#10817)", async () => {
    serve(doc({ generating: true, generating_started_at: "2026-11-10T13:00:00Z" }));
    renderAbout();
    await screen.findByTestId("fleet-about");
    const regenerate = screen.getByTestId("about-regenerate");
    expect(regenerate.getAttribute("aria-busy")).toBe("true");
    expect(regenerate.getAttribute("aria-disabled")).toBe("true");
    expect(regenerate.textContent).toContain("Generating the documentation");
    fireEvent.click(regenerate);
    expect(post).not.toHaveBeenCalled();
  });

  it("shows the new version once the server's generation ends, without a reload (#10817)", async () => {
    let current = doc({ generating: true });
    get.mockImplementation(async (url: string) => (url.endsWith("/documentation") ? current : { versions: [] }));
    renderAbout();
    await screen.findByTestId("fleet-about");
    expect(screen.getByTestId("about-regenerate").getAttribute("aria-busy")).toBe("true");
    // The server's generation lands: the next poll (5 s) reads the new version.
    current = doc({ version: 3, generating: false });
    await waitFor(() => expect(screen.getByTestId("about-author").textContent).toContain("version 3"), { timeout: 7_000 });
    expect(screen.getByTestId("about-regenerate").hasAttribute("aria-busy")).toBe(false);
  }, 10_000);

  it("reads a failed server generation on mount and offers Regenerate again (#10817)", async () => {
    serve(doc({ generating: false, generation_failed: "The documentation could not be generated." }));
    renderAbout();
    const failed = await screen.findByTestId("about-generation-failed");
    expect(failed.textContent).toContain("could not be generated");
    const regenerate = screen.getByTestId("about-regenerate");
    expect(regenerate.hasAttribute("aria-disabled")).toBe(false);
    expect(regenerate.textContent).toContain("Regenerate documentation");
  });

  it.each([
    ["documented", {}],
    ["out of date", { out_of_date: true }],
    ["failed", { generation_failed: "The documentation could not be generated." }],
    ["generating", { generating: true }],
    ["undocumented", { version: 0, sections: {}, updated_at: null, updated_by: null, local: false }],
    ["undocumented, generating", { version: 0, sections: {}, updated_at: null, updated_by: null, local: false, generating: true }],
  ] as [string, Partial<FleetDocumentation>][])("renders exactly one Regenerate control: %s (Sam, 10 Nov 2026)", async (_state, fields) => {
    serve(doc(fields));
    renderAbout();
    await screen.findByTestId("about-regenerate");
    expect(screen.getAllByTestId("about-regenerate")).toHaveLength(1);
    expect(screen.queryAllByRole("button", { name: /regenerate documentation|generate documentation|generating the documentation/i })).toHaveLength(1);
  });

  it("draws the computed facts under the Generate panel of a fleet with no version (#10316)", async () => {
    serve(doc({ version: 0, sections: {}, updated_at: null, updated_by: null, local: false, structured: { purpose_line: "Triage" } }));
    renderAbout();
    const panel = await screen.findByTestId("about-generate-panel");
    expect(panel.textContent).toContain("No documentation yet");
    expect(screen.getByTestId("about-regenerate").textContent).toContain("Generate documentation");
    expect(screen.getByText("showcase")).toBeTruthy();
  });

  it("draws no showcase when nothing is computed, only the panel (#10316)", async () => {
    serve(doc({ version: 0, sections: {}, updated_at: null, updated_by: null, local: false, structured: null }));
    renderAbout();
    await screen.findByTestId("about-generate-panel");
    expect(screen.queryByText("showcase")).toBeNull();
  });

  it("offers no export before the documentation exists", async () => {
    serve(doc({ version: 0, sections: {}, updated_at: null, updated_by: null, local: false }));
    renderAbout();
    await screen.findByTestId("fleet-about-undocumented");
    expect(screen.queryByTestId("about-export-pdf")).toBeNull();
  });
});
