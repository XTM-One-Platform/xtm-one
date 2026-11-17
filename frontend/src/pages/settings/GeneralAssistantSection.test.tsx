// Settings > AI and models > General Assistant: the skills are a resource of the
// Configure panel, not a block of the tab; the chip row counts them; the
// panel's Save writes the choice to `skill_ids` only when it changed.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { AgentSkill, GeneralAssistantConfig } from "@/types";
import { GeneralAssistantSection } from "./GeneralAssistantSection";

const state = vi.hoisted(() => ({ mutate: vi.fn(), registry: { isReady: true, isError: false }, xtmOne: false }));

vi.mock("@/hooks/api/useSkillApi", () => ({
  useSkills: () => ({
    data: [
      { id: "video", name: "Video Production", description: "How to make a video. ".repeat(20), tags: ["builtin", "slug:video-production"], is_company_managed: true, enabled: true },
      { id: "deck", name: "Presentation Design", description: "How to build a deck.", tags: ["builtin", "slug:presentation-design"], is_company_managed: true, enabled: true },
      { id: "judge", name: "Evaluation Judge", description: "Scoring rubric.", tags: [], is_company_managed: true, enabled: true },
      { id: "admiralty", name: "Source Assessment (Admiralty Scale)", description: "Rate a source's reliability.", tags: [], is_company_managed: true, enabled: true },
      { id: "mine", name: "Personal", description: "Not offered.", tags: [], is_company_managed: false, enabled: true },
    ] as unknown as AgentSkill[],
    isLoading: false,
  }),
}));
vi.mock("@/hooks/api/useChannelBotApi", () => ({
  useChannelBots: () => ({ data: [] }),
  useChannelBotChannelsMap: () => ({ map: {}, loadingMap: {} }),
}));
vi.mock("@/hooks/usePlatformConfig", () => ({
  useIsXtmOne: () => state.xtmOne,
  useIsCommunityEdition: () => false,
  useCustomToolsEnabled: () => true,
}));
vi.mock("@/hooks/useBuiltinTools", () => ({
  useBuiltinTools: () => ({
    offered: () => [],
    describe: (id: string) => ({ id, label: id, description: "", icon: () => null, known: true, agentOnly: false }),
    carries: (ids: readonly string[] | null | undefined, id: string) => (ids ?? []).includes(id),
    canonicalIds: (ids: readonly string[] | null | undefined) => [...(ids ?? [])],
    isLoading: !state.registry.isReady && !state.registry.isError,
    isError: state.registry.isError,
    isReady: state.registry.isReady,
  }),
}));
vi.mock("@/hooks/useCredentialSharingConfirm", () => ({
  useCredentialSharingConfirm: () => [null, async () => true],
}));
vi.mock("@/components/ui/prompt-generator", () => ({ PromptGenerator: () => null }));
vi.mock("@/components/ui/model-select", () => ({ ModelSelect: () => null }));

const CONFIG = {
  linked_agent_id: null,
  system_prompt: "",
  llm_model: "",
  max_iterations: 100,
  knowledge_base_ids: [],
  mcp_server_ids: [],
  custom_tool_ids: [],
  builtin_tool_ids: [],
  integration_bindings: {},
  handover_agent_ids: [],
  handover_types: {},
  skill_ids: null,
  default_skill_slugs: ["video-production", "presentation-design"],
  channel_config: {},
  available_channel_types: null,
  env_managed_fields: [],
} as unknown as GeneralAssistantConfig;

function section(gaConfig: GeneralAssistantConfig, gaEditing: boolean, gaBuiltinToolIds: string[] = []) {
  const noop = () => {};
  return (
    <GeneralAssistantSection
      gaConfig={gaConfig}
      updateGAConfig={{ mutate: state.mutate, isPending: false } as never}
      availableModels={[]}
      integrations={[]}
      mcpServers={[]}
      kbs={[]}
      customTools={[]}
      allAgents={[]}
      gaEditing={gaEditing}
      setGaEditing={noop}
      gaPrompt=""
      setGaPrompt={noop}
      gaModel=""
      setGaModel={noop}
      providers={[]}
      gaMaxIterations={100}
      setGaMaxIterations={noop}
      gaKbIds={[]}
      setGaKbIds={noop}
      gaMcpIds={[]}
      setGaMcpIds={noop}
      gaToolIds={[]}
      setGaToolIds={noop}
      gaBuiltinToolIds={gaBuiltinToolIds}
      setGaBuiltinToolIds={noop}
      gaBindings={{}}
      setGaBindings={noop}
      gaHandoverIds={[]}
      setGaHandoverIds={noop}
      gaHandoverTypes={{}}
      setGaHandoverTypes={noop}
    />
  );
}

describe("GeneralAssistantSection skills", () => {
  beforeEach(() => state.mutate.mockClear());

  it("renders no skills block outside the Configure panel and counts the carried skills in the chip row", () => {
    renderWithProviders(section(CONFIG, false));
    expect(screen.queryByTestId("settings.ga.skills")).toBeNull();
    expect(screen.getByTestId("ga-chip-skills").textContent).toContain("2 skills");
  });

  it("counts the admin's choice once one is made", () => {
    renderWithProviders(section({ ...CONFIG, skill_ids: ["judge"] }, false));
    expect(screen.getByTestId("ga-chip-skills").textContent).toContain("1 skill");
  });

  it("lists the company-managed skills inside the panel, the built-in ones attached and badged inline", () => {
    renderWithProviders(section(CONFIG, true));
    const skills = screen.getByTestId("settings.ga.skills");
    expect(skills.closest("fieldset")).not.toBeNull();
    expect(within(skills).queryByText("Personal")).toBeNull();
    const video = within(skills).getByTestId("ga-skill-video");
    expect(video.getAttribute("aria-pressed")).toBe("true");
    // The short chip label, so the skill's name keeps its room on a narrow card.
    expect(within(video).getByText("Built-in").getAttribute("title")).toBe("Built-in for the General Assistant");
    expect(within(video).getByTestId("resource-card-name").textContent).toBe("Video Production");
    expect(within(skills).getByTestId("ga-skill-judge").getAttribute("aria-pressed")).toBe("false");
    // The description sits on the card's own line, under the name.
    expect(within(video).getByTestId("resource-card-description").textContent).toContain("How to make a video.");
  });

  it("filters the skills by name and by description, and says when none matches", () => {
    renderWithProviders(section(CONFIG, true));
    const skills = screen.getByTestId("settings.ga.skills");
    const filter = within(skills).getByPlaceholderText("Filter skills...");

    fireEvent.change(filter, { target: { value: "judge" } });
    expect(within(skills).getByTestId("ga-skill-judge")).toBeTruthy();
    expect(within(skills).queryByTestId("ga-skill-video")).toBeNull();

    fireEvent.change(filter, { target: { value: "reliability" } });
    expect(within(skills).getByTestId("ga-skill-admiralty")).toBeTruthy();
    expect(within(skills).queryByTestId("ga-skill-judge")).toBeNull();

    fireEvent.change(filter, { target: { value: "nothing like this" } });
    expect(within(skills).getByText("No skills match your filter.")).toBeTruthy();
  });

  it("keeps a filtered-out attached skill in the saved choice", () => {
    renderWithProviders(section(CONFIG, true));
    const skills = screen.getByTestId("settings.ga.skills");
    fireEvent.change(within(skills).getByPlaceholderText("Filter skills..."), { target: { value: "judge" } });
    fireEvent.click(within(skills).getByTestId("ga-skill-judge"));
    fireEvent.click(screen.getByText("Save"));
    return vi.waitFor(() => {
      expect(state.mutate).toHaveBeenCalledTimes(1);
      expect([...state.mutate.mock.calls[0][0].skill_ids].sort()).toEqual(["deck", "judge", "video"]);
    });
  });

  it("puts the card's full name first in its tooltip, then the description", async () => {
    renderWithProviders(section(CONFIG, true));
    const card = within(screen.getByTestId("settings.ga.skills")).getByTestId("ga-skill-admiralty");
    fireEvent.focus(card);
    const tip = await screen.findByRole("tooltip");
    expect(tip.textContent).toMatch(/^Source Assessment \(Admiralty Scale\)Rate a source's reliability\./);
  });

  it("saves the skill choice with the panel, on skill_ids, only when it changed", () => {
    const { rerender } = renderWithProviders(section(CONFIG, false));
    fireEvent.click(screen.getByText("Configure"));
    rerender(section(CONFIG, true));
    fireEvent.click(screen.getByTestId("ga-skill-judge"));
    fireEvent.click(screen.getByText("Save"));
    return vi.waitFor(() => {
      expect(state.mutate).toHaveBeenCalledTimes(1);
      const payload = state.mutate.mock.calls[0][0];
      expect([...payload.skill_ids].sort()).toEqual(["deck", "judge", "video"]);
    });
  });

  it("leaves skill_ids out of the save when the choice did not change", () => {
    renderWithProviders(section(CONFIG, true));
    fireEvent.click(screen.getByText("Save"));
    return vi.waitFor(() => {
      expect(state.mutate).toHaveBeenCalledTimes(1);
      expect("skill_ids" in state.mutate.mock.calls[0][0]).toBe(false);
    });
  });
});

// The built-in registry is served: before it answers, or when its read
// fails, the picker shows a skeleton or an error line, never raw ids with a
// remove button on tools that are in fact offered (B1/B3).
describe("GeneralAssistantSection built-in tools before the registry is known", () => {
  beforeEach(() => {
    state.registry = { isReady: true, isError: false };
  });

  it("lists no 'Also enabled' box and shows a skeleton while the registry loads", () => {
    state.registry = { isReady: false, isError: false };
    renderWithProviders(section(CONFIG, true, ["generate_file", "web_fetch"]));
    expect(screen.queryByText("Also enabled, not offered here:")).toBeNull();
    expect(screen.queryByText("generate_file")).toBeNull();
    expect(screen.getByTestId("ga-builtin-tools-loading")).not.toBeNull();
  });

  it("shows an inline error line and keeps the page when the registry read failed", () => {
    state.registry = { isReady: false, isError: true };
    renderWithProviders(section(CONFIG, true, ["generate_file"]));
    expect(screen.getByTestId("ga-builtin-tools-error").textContent).toContain("Built-in tools could not be loaded");
    expect(screen.queryByText("Also enabled, not offered here:")).toBeNull();
    // The rest of the panel is still there.
    expect(screen.getByTestId("settings.ga.skills")).not.toBeNull();
  });

  it("lists a carried id the loaded registry does not offer", () => {
    renderWithProviders(section(CONFIG, true, ["generate_file"]));
    expect(screen.getByText("Also enabled, not offered here:")).not.toBeNull();
  });
});

// Channel routing is a generic assistant setting: editable in every platform
// mode (dev-docs/mode-split.md, S4). In XTM One mode the linked CTEM Assistant
// answers on the enabled channels in the General Assistant's place.
describe("GeneralAssistantSection channel routing in every platform mode", () => {
  beforeEach(() => {
    state.registry = { isReady: true, isError: false };
  });

  it.each([false, true])("opens the channels editor when xtmOne is %s", (xtmOne) => {
    state.xtmOne = xtmOne;
    try {
      renderWithProviders(section(CONFIG, false));
      expect(screen.queryByText("Channel routing is not available in XTM One mode")).toBeNull();
      const edit = screen.getByRole("button", { name: "Edit" });
      expect(edit.hasAttribute("disabled")).toBe(false);
      fireEvent.click(edit);
      expect(screen.getByRole("button", { name: "Cancel" })).not.toBeNull();
    } finally {
      state.xtmOne = false;
    }
  });
});
