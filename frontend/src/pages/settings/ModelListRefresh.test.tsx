// A capability's model picker asks its providers again when it opens:
// a model a provider added or dropped since Settings loaded is offered as it is
// now, and the picker says while it is being read.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MousePointerClick } from "lucide-react";

import { renderWithProviders } from "@/testing/providers";
import type { GeneralAssistantConfig, LLMProvider } from "@/types";
import { MusicGenerationSection } from "./MusicGenerationSection";
import { typedDecisionProviders } from "@/lib/utils";
import { PlatformModelSetting } from "./PlatformModelSetting";
import { SpeechGenerationSection } from "./SpeechGenerationSection";

const state = vi.hoisted(() => ({ fetching: false, refetchSpeech: vi.fn(), refetchMusic: vi.fn() }));

vi.mock("@/hooks/api/useSettingsApi", () => ({
  useAvailableSpeechModels: () => ({
    data: [{ model: "gemini-3.8-flash-tts", provider: "gemini", provider_id: "gemini-id" }],
    isFetching: state.fetching,
    refetch: state.refetchSpeech,
  }),
  useAvailableMusicModels: () => ({
    data: [{ model: "lyria-3.5", provider: "gemini", provider_id: "gemini-id" }],
    isFetching: state.fetching,
    refetch: state.refetchMusic,
  }),
  useMediaModelsInUse: () => ({ data: undefined }),
  useSpeechVoices: () => ({ data: { voices: [] }, isLoading: false }),
  useUpdateGeneralAssistantConfig: () => ({ mutate: vi.fn(), isPending: false }),
}));

const CONFIG = {} as GeneralAssistantConfig;

describe("a model picker opening", () => {
  beforeEach(() => {
    state.fetching = false;
    state.refetchSpeech.mockClear();
    state.refetchMusic.mockClear();
  });

  it("asks for the speech models again and drops the voices read before", () => {
    const { client } = renderWithProviders(
      <SpeechGenerationSection gaConfig={CONFIG} providers={[]} isAdmin canAddProviders envLocked={false} />,
    );
    const invalidated = vi.spyOn(client, "invalidateQueries");
    fireEvent.click(screen.getByText("Configure"));
    expect(state.refetchSpeech).toHaveBeenCalledTimes(1);
    expect(invalidated).toHaveBeenCalledWith({ queryKey: ["speech-voices"] });
  });

  it("asks for the music models again and says so while they are read", () => {
    const view = (
      <MusicGenerationSection gaConfig={CONFIG} providers={[]} isAdmin canAddProviders envLocked={false} />
    );
    const { rerender } = renderWithProviders(view);
    fireEvent.click(screen.getByText("Configure"));
    expect(state.refetchMusic).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).toBeNull();

    state.fetching = true;
    rerender(view);
    expect(screen.getByRole("status").textContent).toContain("Reading the models the providers serve now...");
  });

  it("calls onOpen of a platform model setting as its picker opens", () => {
    const onOpen = vi.fn();
    const setting = (refreshing: boolean) => (
      <PlatformModelSetting
        fieldId="decision"
        icon={MousePointerClick}
        tileClassName=""
        iconClassName=""
        title="Decision Model"
        description="Chooses each click."
        emptyMessage="No decision model available."
        pickHint="Decision models pick among options."
        noneLabel="Not configured"
        models={[{ model: "clef-flash", provider: "ollama", provider_id: "ollama-id" }]}
        setSubtitle=""
        unsetSubtitle="Not set"
        envLocked={false}
        canEdit
        isSaving={false}
        onSave={vi.fn()}
        onOpen={onOpen}
        refreshing={refreshing}
      />
    );
    const { rerender } = renderWithProviders(setting(false));
    fireEvent.click(screen.getByText("Configure"));
    expect(onOpen).toHaveBeenCalledTimes(1);

    rerender(setting(true));
    expect(screen.getByRole("status").textContent).toContain("Reading the models the providers serve now...");
  });

  it("types a decision model id only for a provider that cannot list them", () => {
    const providers = [
      { id: "gw-id", provider: "filigran_gateway", capabilities: ["chat", "decision"], lists_decision_models: true },
      { id: "or-id", provider: "openrouter", capabilities: ["chat", "decision"], lists_decision_models: false },
      { id: "an-id", provider: "anthropic", capabilities: ["chat"], lists_decision_models: false },
    ] as LLMProvider[];
    expect(typedDecisionProviders(providers).map((p) => p.id)).toEqual(["or-id"]);
  });

  describe("a saved model its provider does not list", () => {
    const saveUntouched = (props: Partial<Parameters<typeof PlatformModelSetting>[0]>) => {
      const onSave = vi.fn();
      renderWithProviders(
        <PlatformModelSetting
          fieldId="decision"
          icon={MousePointerClick}
          tileClassName=""
          iconClassName=""
          title="Decision Model"
          description="Chooses each click."
          emptyMessage="No decision model available."
          pickHint="Decision models pick among options."
          noneLabel="Not configured"
          models={[]}
          model="clef-flash"
          providerId="gw-id"
          setSubtitle="Decision model"
          unsetSubtitle="Not set"
          envLocked={false}
          canEdit
          isSaving={false}
          onSave={onSave}
          {...props}
        />,
      );
      fireEvent.click(screen.getByText("Configure"));
      fireEvent.click(screen.getByText("Save"));
      return onSave;
    };

    it("is kept by a picker whose listing may be partial", () => {
      expect(saveUntouched({ keepUnlisted: true })).toHaveBeenCalledWith("clef-flash", "gw-id", expect.any(Function));
    });

    it("is dropped by any other picker, as a retired model", () => {
      expect(saveUntouched({})).toHaveBeenCalledWith("", null, expect.any(Function));
    });

    it("is dropped once its provider config is deleted", () => {
      const providers = [{ id: "other-id", provider: "openai" }] as LLMProvider[];
      expect(saveUntouched({ keepUnlisted: true, providers })).toHaveBeenCalledWith("", null, expect.any(Function));
    });
  });

  it("says the models are being read until the list first answers, not on each refetch", () => {
    const card = (loading: boolean) => (
      <PlatformModelSetting
        fieldId="decision"
        icon={MousePointerClick}
        tileClassName=""
        iconClassName=""
        title="Decision Model"
        description="Chooses each click."
        emptyMessage="No decision model available."
        pickHint="Decision models pick among options."
        noneLabel="Not configured"
        models={[]}
        setSubtitle=""
        unsetSubtitle="Not set"
        envLocked={false}
        canEdit
        isSaving={false}
        onSave={vi.fn()}
        loading={loading}
        refreshing
      />
    );
    const { rerender } = renderWithProviders(card(true));
    expect(screen.getByRole("status").textContent).toContain("Reading the models the providers serve now...");
    expect(screen.queryByText("No decision model available.")).toBeNull();

    rerender(card(false));
    expect(screen.getByText("No decision model available.")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
