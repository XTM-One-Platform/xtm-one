// The companion's Inbox: the server's feed, waiting runs answered in place,
// the rest opening their page.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { NotificationFeed } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));

import { CompanionInbox } from "./CompanionInbox";

const RUN = "11111111-1111-4111-8111-111111111111";

const FEED: NotificationFeed = {
  unread_count: 2,
  read_at: null,
  items: [
    {
      id: RUN,
      kind: "hitl",
      created_at: new Date().toISOString(),
      route: `/operations/executions/${RUN}`,
      title: "Containment",
      subtitle: "Remediation planner",
      summary: "Approval required before running: isolate_host",
      critical: false,
      run_id: RUN,
      unread: true,
    },
    {
      id: "22222222-2222-4222-8222-222222222222",
      kind: "reminder",
      created_at: new Date().toISOString(),
      route: "/runs",
      title: "Check the weekly brief",
      subtitle: "Reminder",
      summary: null,
      critical: false,
      run_id: null,
      unread: true,
    },
  ],
};

function draw() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/inbox"]}>
      <Routes>
        <Route path="/inbox" element={<CompanionInbox />} />
        <Route path="/runs" element={<p>Runs page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CompanionInbox", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
    api.get.mockImplementation(async (path: string) => {
      if (path === "/notifications") return FEED;
      if (path === `/assignments/runs/${RUN}/pending-approvals`) {
        return {
          run_id: RUN,
          status: "awaiting_human",
          kind: "action_proposal",
          question: null,
          proposals: [
            {
              tool_call_id: "call-1",
              tool_name: "isolate_host",
              tool_description: "Isolate a host from the network",
              arguments: {},
              input_schema: {},
              source: "integration",
            },
          ],
        };
      }
      throw new Error(`unexpected ${path}`);
    });
    api.post.mockResolvedValue({});
  });

  it("answers a waiting run in place with the web app's own approval list", async () => {
    draw();
    expect(await screen.findByText("Containment")).toBeTruthy();
    const approve = await screen.findByRole("button", { name: "Yes" });
    fireEvent.click(approve);
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(`/assignments/runs/${RUN}/respond`, {
        decisions: [expect.objectContaining({ tool_call_id: "call-1", decision: "approve" })],
      }),
    );
  });

  it("opens an item's page and marks it read", async () => {
    draw();
    fireEvent.click(await screen.findByRole("button", { name: /Check the weekly brief/ }));
    expect(await screen.findByText("Runs page")).toBeTruthy();
    expect(api.post).toHaveBeenCalledWith("/notifications/22222222-2222-4222-8222-222222222222/dismiss");
  });

  it("opens a waiting work item without reading it, its subtitle translated (#991)", async () => {
    const ITEM = "33333333-3333-4333-8333-333333333333";
    api.get.mockImplementation(async (path: string) => {
      if (path === "/notifications") {
        return {
          unread_count: 1,
          read_at: null,
          items: [
            {
              id: `work_item:${ITEM}`,
              kind: "work_item_waiting",
              created_at: new Date().toISOString(),
              route: "/runs",
              title: "Approve the rollout",
              subtitle: "Work item",
              summary: "Ship on Monday?",
              critical: false,
              run_id: null,
              awaiting_answer: true,
              unread: true,
            },
          ],
        } satisfies NotificationFeed;
      }
      throw new Error(`unexpected ${path}`);
    });
    draw();
    expect(await screen.findByText("Work item")).toBeTruthy();
    // Every unread entry is unread until answered: nothing to mark read.
    expect(screen.queryByRole("button", { name: "Mark all read" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Approve the rollout/ }));
    expect(await screen.findByText("Runs page")).toBeTruthy();
    // A decision still waiting is read only by its answer (#991): no dismiss call.
    expect(api.post).not.toHaveBeenCalled();
  });

  it("marks everything read on the server", async () => {
    api.post.mockResolvedValueOnce({ ...FEED, unread_count: 0 });
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Mark all read" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/notifications/read", {}));
  });
});
