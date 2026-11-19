// Profile > Health companion (#8261): off by default, turned on and pointed
// at a pet through the user's own route; built in since #101 (S1), no flag.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), download: vi.fn() }));
const auth = vi.hoisted(() => ({ admin: true }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: auth.admin }) }));

import { petUploadErrorKey } from "@/lib/health-pet";
import { HealthCompanionCard } from "./HealthCompanionCard";

const PETS = [
  { slug: "butler", display_name: "Butler", description: null, sprite_version: 1, builtin: true, sha256: "a" },
  { slug: "owl", display_name: "Owl", description: "A night owl.", sprite_version: 2, builtin: false, sha256: "b" },
];

describe("HealthCompanionCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.admin = true;
    globalThis.URL.createObjectURL = vi.fn(() => "blob:pet");
    globalThis.URL.revokeObjectURL = vi.fn();
    api.get.mockImplementation((path: string) => {
      if (path === "/health/me/companion") return Promise.resolve({ enabled: false, pet: "butler", default_pet: "butler" });
      if (path === "/health/pets") return Promise.resolve(PETS);
      if (path === "/health/name") return Promise.resolve({ name: "Platform Health", default_name: "Platform Health" });
      return Promise.reject(new Error(path));
    });
    api.download.mockResolvedValue({ blob: new Blob(["x"]), filename: null });
    api.put.mockImplementation((_path: string, body: { enabled: boolean; pet: string }) =>
      Promise.resolve({ ...body, default_pet: "butler" }),
    );
  });

  it("is off by default and turns on with the pet shown", async () => {
    renderWithProviders(<HealthCompanionCard />);
    const toggle = await screen.findByRole("switch", { name: "Show the companion" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    fireEvent.click(toggle);
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/health/me/companion", { enabled: true, pet: "butler" }));
    expect(api.put).toHaveBeenCalledTimes(1);
  });

  it("picks a pet of the library", async () => {
    renderWithProviders(<HealthCompanionCard />);
    expect(await screen.findByRole("radio", { name: /Butler/ })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(await screen.findByRole("radio", { name: /Owl/ }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/health/me/companion", { enabled: false, pet: "owl" }));
  });

  it("is not offered to a plain user, who cannot open Platform Health (#101)", async () => {
    auth.admin = false;
    renderWithProviders(<HealthCompanionCard />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId("profile.health-companion")).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("reads the companion with no feature flag (#101 S1)", async () => {
    renderWithProviders(<HealthCompanionCard />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/health/me/companion"));
    expect(await screen.findByTestId("profile.health-companion")).toBeInTheDocument();
  });
});

describe("petUploadErrorKey", () => {
  it("names the rule of a known code and nothing for any other failure", () => {
    expect(petUploadErrorKey({ detail: { code: "image_size" } })).toBe(
      "The spritesheet must be 1536x1872 (version 1) or 1536x2288 (version 2).",
    );
    expect(petUploadErrorKey({ detail: { code: "constructor" } })).toBeNull();
    expect(petUploadErrorKey({ detail: "A pet with this id" })).toBeNull();
    expect(petUploadErrorKey(new Error("boom"))).toBeNull();
    expect(petUploadErrorKey(null)).toBeNull();
  });
});
