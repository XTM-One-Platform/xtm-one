// The Video Generation section when DEFAULT_VIDEO_PROVIDER pins it: an
// Env badge instead of Configure, and a retired pinned model points at the
// environment, not at a button that is not there. Opening the picker asks the
// providers for their video models again.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { GeneralAssistantConfig } from "@/types";
import { VideoGenerationSection } from "./VideoGenerationSection";

const state = vi.hoisted(() => ({ unavailable: null as string | null, fetching: false, refetch: vi.fn() }));

vi.mock("@/hooks/api/useSettingsApi", () => ({
  useAvailableVideoModels: () => ({
    data: [{ model: "veo-3.1-generate-preview", provider: "gemini", provider_id: "gemini-id" }],
    isFetching: state.fetching,
    refetch: state.refetch,
  }),
  useUpdateGeneralAssistantConfig: () => ({ mutate: vi.fn(), isPending: false }),
  useVideoModelStatus: () => ({ data: { unavailable: state.unavailable } }),
}));

const PINNED = {
  video_provider: "openai",
  video_provider_id: "openai-id",
  video_model: "sora-2",
} as GeneralAssistantConfig;

function section(envLocked: boolean) {
  return (
    <VideoGenerationSection gaConfig={PINNED} providers={[]} isAdmin canAddProviders envLocked={envLocked} />
  );
}

describe("VideoGenerationSection", () => {
  beforeEach(() => {
    state.unavailable = null;
    state.fetching = false;
    state.refetch.mockClear();
  });

  it("shows the Env badge instead of Configure while the environment pins it", () => {
    renderWithProviders(section(true));
    expect(screen.getByText("Env")).toBeTruthy();
    expect(screen.queryByText("Configure")).toBeNull();
  });

  it("closes a form opened before the lock arrived", () => {
    const { rerender } = renderWithProviders(section(false));
    fireEvent.click(screen.getByText("Configure"));
    expect(screen.getByText("Save")).toBeTruthy();

    rerender(section(true));
    expect(screen.queryByText("Save")).toBeNull();
    expect(screen.getByText("Env")).toBeTruthy();
  });

  it("tells to change the variable when the pinned model is retired", () => {
    state.unavailable = "OpenAI retired sora-2";
    renderWithProviders(section(true));
    expect(
      screen.getByText(/Set by the environment: change DEFAULT_VIDEO_MODEL \(or DEFAULT_VIDEO_PROVIDER\)/),
    ).toBeTruthy();
    expect(screen.queryByText(/Click "Configure"/)).toBeNull();
  });

  it("offers Configure for a retired model nobody pinned", () => {
    state.unavailable = "OpenAI retired sora-2";
    renderWithProviders(section(false));
    expect(screen.getByText(/Click "Configure" to pick a live video model\./)).toBeTruthy();
  });

  it("asks the providers for their video models again when the picker opens, and says so", () => {
    const { rerender } = renderWithProviders(section(false));
    expect(state.refetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Configure"));
    expect(state.refetch).toHaveBeenCalledTimes(1);

    state.fetching = true;
    rerender(section(false));
    expect(screen.getByRole("status").textContent).toContain("Reading the models the providers serve now...");
  });
});
