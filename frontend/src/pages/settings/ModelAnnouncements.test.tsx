// What the User announcements panel says an announcement carried, the
// capabilities a deployment's environment pins included.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { ModelAnnouncementSummary } from "@/types";
import { AnnouncedModels } from "./ModelAnnouncements";

const LATEST = {
  id: "a",
  source: "env",
  status: "pending",
  skip_reason: null,
  default_model: null,
  previous_default_model: null,
  added_models: [],
  removed_models: [],
  capabilities: [],
  created_at: "2026-10-17T09:00:00+00:00",
  sent_at: null,
  delivered: 0,
  refused: 0,
  unknown: 0,
  remaining: 12,
  queued_behind: 0,
  retry_at: null,
  failed_attempts: 0,
  last_error: null,
  held_until: null,
  blocked_until: null,
  deferred: 0,
  deferred_retry_at: null,
  deferred_retrying: false,
  last_refusal: null,
} satisfies ModelAnnouncementSummary;

describe("AnnouncedModels", () => {
  it("names each capability by its Settings section, with its model and the one it replaces", () => {
    renderWithProviders(
      <AnnouncedModels
        latest={{
          ...LATEST,
          capabilities: [
            { capability: "video", model: "veo-3.1-generate-preview", provider: "gemini", previous_model: null },
            { capability: "speech", model: "gpt-4o-mini-tts", provider: "openai", previous_model: "tts-1-hd" },
          ],
        }}
      />,
    );
    expect(screen.getByText("Video Generation")).toBeTruthy();
    expect(screen.getByText("veo-3.1-generate-preview")).toBeTruthy();
    expect(screen.getByText("Speech Generation")).toBeTruthy();
    expect(screen.getByText("gpt-4o-mini-tts")).toBeTruthy();
    expect(screen.getByText("replaces tts-1-hd")).toBeTruthy();
    // A capability new to the platform replaces nothing.
    expect(screen.getAllByText(/^replaces /)).toHaveLength(1);
  });

  it("shows the model news and the capabilities side by side", () => {
    renderWithProviders(
      <AnnouncedModels
        latest={{
          ...LATEST,
          default_model: "claude-sonnet-5-5",
          previous_default_model: "claude-sonnet-5",
          capabilities: [{ capability: "music", model: "lyria-3.5", provider: "gemini", previous_model: null }],
        }}
      />,
    );
    expect(screen.getByText("claude-sonnet-5-5")).toBeTruthy();
    expect(screen.getByText("replaces claude-sonnet-5")).toBeTruthy();
    expect(screen.getByText("Music Generation")).toBeTruthy();
    expect(screen.getByText("lyria-3.5")).toBeTruthy();
  });

  it("says nothing of capabilities an announcement does not carry", () => {
    renderWithProviders(<AnnouncedModels latest={{ ...LATEST, added_models: ["claude-opus-5-5"] }} />);
    expect(screen.getByText("claude-opus-5-5")).toBeTruthy();
    expect(screen.queryByText("Video Generation")).toBeNull();
  });
});
