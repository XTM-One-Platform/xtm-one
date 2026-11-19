// The Health companion (#8261): shown only to an administrator who turned it on
// (#101: /health is admin-only), says what Platform Health found, holds still
// under reduced motion, and hides through the user's own route.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), download: vi.fn() }));
const flags = vi.hoisted(() => ({ reduced: false, admin: true }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: flags.admin }) }));
vi.mock("@/hooks/usePrefersReducedMotion", () => ({ usePrefersReducedMotion: () => flags.reduced }));

import { HealthCompanion } from "./HealthCompanion";

const PET = { slug: "butler", display_name: "Butler", description: null, sprite_version: 1, builtin: true, sha256: "abc" };

function serve(
  companion: { enabled: boolean; pet: string },
  state = "review",
  findings = { critical: 0, warning: 0, info: 0 },
) {
  api.get.mockImplementation((path: string) => {
    if (path === "/health/me/companion") return Promise.resolve({ ...companion, default_pet: "butler" });
    if (path === "/health/pets") return Promise.resolve([PET]);
    if (path === "/health/me/activity") return Promise.resolve({ state, running: 0, waiting: 0, review: 1, failed_at: null, ...findings });
    if (path === "/health/name") return Promise.resolve({ name: "Jeeves", default_name: "Concierge" });
    return Promise.reject(new Error(path));
  });
  api.download.mockResolvedValue({ blob: new Blob(["x"], { type: "image/webp" }), filename: null });
}

describe("HealthCompanion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.reduced = false;
    flags.admin = true;
    globalThis.URL.createObjectURL = vi.fn(() => "blob:pet");
    globalThis.URL.revokeObjectURL = vi.fn();
  });

  it("shows nothing while the companion is off", async () => {
    serve({ enabled: false, pet: "butler" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/health/me/companion"));
    expect(screen.queryByTestId("health.companion")).toBeNull();
    expect(api.get).not.toHaveBeenCalledWith("/health/me/activity");
  });

  it("shows nothing to a plain user and calls no companion route (#101)", async () => {
    flags.admin = false;
    serve({ enabled: true, pet: "butler" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId("health.companion")).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("says the open critical findings first, then the state", async () => {
    serve({ enabled: true, pet: "butler" }, "running", { critical: 2, warning: 3, info: 1 });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "2 critical findings are open. Jeeves is checking the platform.",
    );
  });

  it("says when nothing is open, with no idle filler", async () => {
    serve({ enabled: true, pet: "butler" }, "idle");
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByRole("status")).toHaveTextContent(/^No open finding\.$/);
  });

  it("reads the companion with no feature flag (#101 S1)", async () => {
    serve({ enabled: true, pet: "butler" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByTestId("health.companion")).toBeInTheDocument();
  });

  it("says what it shows, with the Platform Health's name", async () => {
    serve({ enabled: true, pet: "butler" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByRole("status")).toHaveTextContent("No open finding. A proposal is ready for your review.");
    fireEvent.click(await screen.findByRole("button", { name: "Butler" }));
    expect(await screen.findByRole("link", { name: "Open Jeeves" })).toHaveAttribute("href", "/health");
  });

  it("skips the greeting and holds the state's first frame under reduced motion", async () => {
    flags.reduced = true;
    serve({ enabled: true, pet: "butler" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    const sprite = await screen.findByRole("img", { name: "Butler, the companion" });
    // Review (row 8) at half scale, column 0: never the waving row (3).
    expect(sprite.style.backgroundPosition).toBe("0px -832px");
  });

  it("shows the default pet when the chosen one left the library", async () => {
    serve({ enabled: true, pet: "night-owl" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByRole("img", { name: "Butler, the companion" })).toBeInTheDocument();
  });

  it("hides through the user's own companion route", async () => {
    serve({ enabled: true, pet: "butler" });
    api.put.mockResolvedValue({ enabled: false, pet: "butler", default_pet: "butler" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Butler" }));
    fireEvent.click(await screen.findByRole("button", { name: "Hide" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/health/me/companion", { enabled: false, pet: "butler" }));
    await waitFor(() => expect(screen.queryByTestId("health.companion")).toBeNull());
  });
});
