// With no image or speech model saved, the cards name the model generation
// really runs on (a built-in or AI Gateway default, #1816), marked automatic.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { AvailableModel, GeneralAssistantConfig } from "@/types";
import { ImageModelSummary } from "./ImageModelSummary";
import { SpeechGenerationSection } from "./SpeechGenerationSection";

const state = vi.hoisted(() => ({
  inUse: { image: null, speech: null } as { image: AvailableModel | null; speech: AvailableModel | null },
  enabled: [] as boolean[],
  listed: [{ model: "tts-1-hd", provider: "filigran_gateway", provider_id: "gw" }] as AvailableModel[],
}));

vi.mock("@/hooks/api/useSettingsApi", () => ({
  useAvailableSpeechModels: () => ({
    data: state.listed,
    isFetching: false,
    refetch: vi.fn(),
  }),
  useMediaModelsInUse: (enabled: boolean) => {
    state.enabled.push(enabled);
    return { data: enabled ? state.inUse : undefined };
  },
  useSpeechVoices: () => ({ data: { voices: [] }, isLoading: false }),
  useUpdateGeneralAssistantConfig: () => ({ mutate: vi.fn(), isPending: false }),
}));

const GATEWAY_TTS = { model: "tts-1-hd", provider: "filigran_gateway", provider_id: "gw" };
const GATEWAY_IMAGE = { model: "gemini-3-pro-image-preview", provider: "filigran_gateway", provider_id: "gw" };

const speech = (config: Partial<GeneralAssistantConfig>) =>
  renderWithProviders(
    <SpeechGenerationSection
      gaConfig={config as GeneralAssistantConfig}
      providers={[]}
      isAdmin
      canAddProviders
      envLocked={false}
    />,
  );

describe("the speech card", () => {
  beforeEach(() => {
    state.inUse = { image: null, speech: GATEWAY_TTS };
    state.enabled = [];
    state.listed = [GATEWAY_TTS];
  });

  it("names the model in use while none is saved", () => {
    speech({});
    expect(screen.getByText("tts-1-hd")).toBeTruthy();
    expect(screen.getByText("Speech Provider: Filigran AI Gateway")).toBeTruthy();
    expect(screen.getByText("Automatic")).toBeTruthy();
    expect(screen.queryByText("Auto (provider default)")).toBeNull();
  });

  it("keeps the saved model and asks for nothing else", () => {
    speech({ speech_model: "gpt-4o-mini-tts", speech_provider: "openai" });
    expect(screen.getByText("gpt-4o-mini-tts")).toBeTruthy();
    expect(screen.queryByText("Automatic")).toBeNull();
    expect(state.enabled.every((e) => e === false)).toBe(true);
  });

  it("shows the model in use even when no speech model is listed", () => {
    state.listed = [];
    speech({});
    expect(screen.getByText("tts-1-hd")).toBeTruthy();
    expect(screen.getByText("Automatic")).toBeTruthy();
  });

  it("says auto when nothing can speak", () => {
    state.inUse = { image: null, speech: null };
    speech({});
    expect(screen.getByText("Auto (provider default)")).toBeTruthy();
    expect(screen.queryByText("Automatic")).toBeNull();
  });
});

describe("the image card", () => {
  it("names the model in use while none is saved", () => {
    renderWithProviders(<ImageModelSummary model={null} providerName={null} inUse={GATEWAY_IMAGE} providers={[]} />);
    expect(screen.getByText("gemini-3-pro-image-preview")).toBeTruthy();
    expect(screen.getByText("Image Provider: Filigran AI Gateway")).toBeTruthy();
    expect(screen.getByText("Automatic")).toBeTruthy();
  });

  it("keeps the saved model, active", () => {
    renderWithProviders(
      <ImageModelSummary model="gpt-image-2" providerName="OpenAI" inUse={GATEWAY_IMAGE} providers={[]} />,
    );
    expect(screen.getByText("gpt-image-2")).toBeTruthy();
    expect(screen.getByText("Image Provider: OpenAI")).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.queryByText("gemini-3-pro-image-preview")).toBeNull();
  });

  it("says none is selected when nothing can make images", () => {
    renderWithProviders(<ImageModelSummary model={null} providerName={null} inUse={null} providers={[]} />);
    expect(screen.getByText("No image model selected")).toBeTruthy();
    expect(screen.getByText('Click "Configure" to pick a default image model')).toBeTruthy();
  });
});
