// Profile > Reply suggestions: on by default, turned off through the user's
// own preferences route (backend/app/services/chat_reply_suggestions.py).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));

import { ReplySuggestionsCard } from "./ReplySuggestionsCard";

describe("ReplySuggestionsCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.put.mockResolvedValue({});
  });

  it("is on when the person never chose, and turns off", async () => {
    api.get.mockResolvedValue({ id: "u1", role: "user" });
    renderWithProviders(<ReplySuggestionsCard />);
    const toggle = await screen.findByRole("switch", { name: "Suggest my next message" });
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/auth/me"));
    expect(toggle).toHaveAttribute("aria-checked", "true");
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith("/settings/preferences", { reply_suggestions_enabled: false }),
    );
  });

  it("shows the person's choice once off", async () => {
    api.get.mockResolvedValue({ id: "u1", role: "user", reply_suggestions_enabled: false });
    renderWithProviders(<ReplySuggestionsCard />);
    const toggle = await screen.findByRole("switch", { name: "Suggest my next message" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
  });
});
