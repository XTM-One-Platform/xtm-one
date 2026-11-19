// The Health companion (#8261): shown only to who turned it on, says what it
// shows, holds still under reduced motion, and hides through the user's own route.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), download: vi.fn() }));
const flags = vi.hoisted(() => ({ reduced: false }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/usePrefersReducedMotion", () => ({ usePrefersReducedMotion: () => flags.reduced }));

import { HealthCompanion } from "./HealthCompanion";

const PET = { slug: "alfred", display_name: "Alfred", description: null, sprite_version: 1, builtin: true, sha256: "abc" };

function serve(companion: { enabled: boolean; pet: string }, state = "review") {
  api.get.mockImplementation((path: string) => {
    if (path === "/health/me/companion") return Promise.resolve({ ...companion, default_pet: "alfred" });
    if (path === "/health/pets") return Promise.resolve([PET]);
    if (path === "/health/me/activity") return Promise.resolve({ state, running: 0, waiting: 0, review: 1, failed_at: null });
    if (path === "/health/name") return Promise.resolve({ name: "Jeeves", default_name: "Concierge" });
    return Promise.reject(new Error(path));
  });
  api.download.mockResolvedValue({ blob: new Blob(["x"], { type: "image/webp" }), filename: null });
}

describe("HealthCompanion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.reduced = false;
    globalThis.URL.createObjectURL = vi.fn(() => "blob:pet");
    globalThis.URL.revokeObjectURL = vi.fn();
  });

  it("shows nothing while the companion is off", async () => {
    serve({ enabled: false, pet: "alfred" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/health/me/companion"));
    expect(screen.queryByTestId("health.companion")).toBeNull();
    expect(api.get).not.toHaveBeenCalledWith("/health/me/activity");
  });

  it("reads the companion with no feature flag (#101 S1)", async () => {
    serve({ enabled: true, pet: "alfred" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByTestId("health.companion")).toBeInTheDocument();
  });

  it("says what it shows, with the Platform Health's name", async () => {
    serve({ enabled: true, pet: "alfred" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByRole("status")).toHaveTextContent("A proposal is ready for your review.");
    fireEvent.click(await screen.findByRole("button", { name: "Alfred" }));
    expect(await screen.findByRole("link", { name: "Open Jeeves" })).toHaveAttribute("href", "/health");
  });

  it("skips the greeting and holds the state's first frame under reduced motion", async () => {
    flags.reduced = true;
    serve({ enabled: true, pet: "alfred" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    const sprite = await screen.findByRole("img", { name: "Alfred, the companion" });
    // Review (row 8) at half scale, column 0: never the waving row (3).
    expect(sprite.style.backgroundPosition).toBe("0px -832px");
  });

  it("shows the default pet when the chosen one left the library", async () => {
    serve({ enabled: true, pet: "night-owl" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    expect(await screen.findByRole("img", { name: "Alfred, the companion" })).toBeInTheDocument();
  });

  it("hides through the user's own companion route", async () => {
    serve({ enabled: true, pet: "alfred" });
    api.put.mockResolvedValue({ enabled: false, pet: "alfred", default_pet: "alfred" });
    renderWithProviders(<MemoryRouter><HealthCompanion /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Alfred" }));
    fireEvent.click(await screen.findByRole("button", { name: "Hide" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/health/me/companion", { enabled: false, pet: "alfred" }));
    await waitFor(() => expect(screen.queryByTestId("health.companion")).toBeNull());
  });
});
