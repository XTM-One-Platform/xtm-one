import { describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { Agent } from "@/types";
import { ChannelsSection } from "./ChannelsSection";

type Props = ComponentProps<typeof ChannelsSection>;

function props(agent: Partial<Agent>, canManage: boolean): Props {
  return {
    agent: {
      id: "agent-1",
      name: "Onboarding",
      web_enabled: true,
      web_token: null,
      web_uploads_enabled: false,
      web_suggested_prompts: [],
      channel_config: null,
      available_channel_types: [],
      ...agent,
    } as Agent,
    canManage,
    isPending: false,
    isAdmin: false,
    onWebToggle: vi.fn(),
    onWebUploadsToggle: vi.fn(),
    embedDialogOpen: false,
    setEmbedDialogOpen: vi.fn(),
    snippetCopied: false,
    embedSnippet: "",
    onCopySnippet: vi.fn(),
    baseUrl: "https://xtm.example",
    suggestedPrompts: ["What is new?"],
    newPromptText: "",
    setNewPromptText: vi.fn(),
    editingSuggested: false,
    onAddSuggestedPrompt: vi.fn(),
    onRemoveSuggestedPrompt: vi.fn(),
    onSaveSuggestedPrompts: vi.fn(),
    onCancelSuggestedPrompts: vi.fn(),
    onGenerateSuggested: vi.fn(),
    isGeneratingSuggested: false,
    editingChannels: false,
    channelConfigDraft: {},
    onToggleChannelBot: vi.fn(),
    onUpdateChannelEntry: vi.fn(),
    availableBots: [],
    restrictedBots: [],
    channelBots: [],
    platformChannelsMap: {},
    channelsLoadingMap: {},
    onStartEditChannels: vi.fn(),
    onSaveChannels: vi.fn(),
    onCancelChannels: vi.fn(),
    onOpenToolScope: vi.fn(),
  };
}

describe("ChannelsSection web widget", () => {
  it("shows a viewer that the widget is live, without its embed code", () => {
    renderWithProviders(<ChannelsSection {...props({ web_token: null }, false)} />);
    expect(screen.getByText("Live")).toBeTruthy();
    expect(screen.getByText("What is new?")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Get Embed Code/ })).toBeNull();
  });

  it("gives a manager the embed code", () => {
    renderWithProviders(<ChannelsSection {...props({ web_token: "public-widget-token" }, true)} />);
    expect(screen.getByText("Live")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Get Embed Code/ })).toBeTruthy();
  });
});
