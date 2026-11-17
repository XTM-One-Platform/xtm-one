// Requests to act on a paused run, made from the card of its pasted link.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { RunActionRequest } from "@/types";
import { RunActionRequests } from "./RunActionRequests";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const request: RunActionRequest = {
  id: "q-1",
  requester_name: "Jane Doe",
  source: "teams_card",
  created_at: "2026-10-27T10:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue([request]);
  post.mockResolvedValue(undefined);
});

describe("RunActionRequests", () => {
  it("lists who asked and dismisses a request", async () => {
    renderWithProviders(<RunActionRequests runId="r-1" enabled />);

    expect(await screen.findByText(/Jane Doe asked you to act on this run\./)).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/assignments/runs/r-1/action-requests");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/assignments/runs/r-1/action-requests/q-1/dismiss"));
  });

  it("says so when a request cannot be dismissed", async () => {
    post.mockRejectedValue(new Error("Request not found"));
    renderWithProviders(<RunActionRequests runId="r-1" enabled />);

    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    expect(await screen.findByText("Could not dismiss the request")).toBeTruthy();
  });

  it("asks nothing and shows nothing to someone who cannot answer the run", () => {
    const { container } = renderWithProviders(<RunActionRequests runId="r-1" enabled={false} />);

    expect(get).not.toHaveBeenCalled();
    expect(container.textContent).toBe("");
  });

  it("shows nothing when nobody asked", async () => {
    get.mockResolvedValue([]);
    const { container } = renderWithProviders(<RunActionRequests runId="r-1" enabled />);

    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    expect(container.textContent).toBe("");
  });
});
