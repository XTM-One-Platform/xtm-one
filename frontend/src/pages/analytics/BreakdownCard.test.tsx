// The Cost breakdown "By agent" table: an agent name links only where
// the server lets the caller open it, as in Top agents; a deleted or a
// non-permitted agent is plain text, a deleted one named with its chip.
// The chip has its own status key: ja reads it as a status.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import en from "@/i18n/lang/en.json";
import ja from "@/i18n/lang/ja.json";
import { renderWithProviders } from "@/testing/providers";
import { BreakdownCard } from "@/pages/analytics/BreakdownCard";
import type { TraceAnalyticsCostByAgentItem } from "@/types";

const saved = vi.hoisted(() => ({ blobs: [] as Blob[] }));
vi.mock("@/lib/utils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/utils")>()),
  saveBlobAs: (blob: Blob) => saved.blobs.push(blob),
}));

function row(overrides: Partial<TraceAnalyticsCostByAgentItem>): TraceAnalyticsCostByAgentItem {
  return {
    agent_id: "a",
    agent_name: "Agent",
    agent_icon: null,
    owner_id: null,
    owner_name: null,
    owner_email: null,
    is_company_managed: false,
    agent_status: "active",
    can_open: false,
    agent_url: null,
    trace_count: 1,
    total_cost: 1,
    avg_cost_per_trace: 1,
    avg_duration_ms: 1,
    avg_feedback_score: null,
    ...overrides,
  };
}

const LONG_NAME = "Quarterly Threat Landscape Correlation and Enrichment Assistant v2";

const ROWS = [
  row({ agent_id: "open", agent_name: "Shared Bot", can_open: true, agent_url: "/agents/open", total_cost: 4 }),
  row({ agent_id: "shut", agent_name: "Private Bot", total_cost: 3 }),
  row({ agent_id: "gone", agent_name: "Old Bot", agent_status: "deleted", can_open: true, agent_url: "/agents/gone", total_cost: 2 }),
  row({ agent_id: "ghost", agent_name: null, agent_status: "deleted", total_cost: 1 }),
  row({ agent_id: "off", agent_name: "Paused Bot", agent_status: "disabled", can_open: true, agent_url: "/agents/off", total_cost: 1 }),
  row({ agent_id: "long", agent_name: LONG_NAME, can_open: true, agent_url: "/agents/long", total_cost: 1 }),
  row({ agent_id: "long-shut", agent_name: `${LONG_NAME} (private)`, total_cost: 1 }),
  row({ agent_id: "corp", agent_name: "Corp Bot", is_company_managed: true, total_cost: 1 }),
  row({ agent_id: "owned", agent_name: "Owned Bot", owner_id: "u1", owner_name: "Ann Example", owner_email: "ann@example.com", total_cost: 1 }),
];

function renderTable(linkEntities: boolean, url = "/observability") {
  const empty = { isLoading: false, isError: false };
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <BreakdownCard
        breakdown="agents"
        onBreakdownChange={() => {}}
        totalCost={11}
        costDataAvailable
        linkEntities={linkEntities}
        agents={{ items: ROWS, ...empty }}
        users={empty}
        groups={empty}
        models={{ items: [], latency: [], ...empty }}
      />
    </MemoryRouter>,
  );
}

function agentLinks() {
  return screen.queryAllByRole("link").filter((el) => el.getAttribute("href")?.startsWith("/agents/"));
}

describe("Cost breakdown by agent", () => {
  it("links only the agents the server lets the caller open, for an administrator too", () => {
    renderTable(true);
    const links = agentLinks();
    expect(links.map((el) => el.getAttribute("href")).sort()).toEqual(["/agents/long", "/agents/off", "/agents/open"]);
    expect(screen.getByText("Private Bot").closest("a")).toBeNull();
    expect(screen.getByText("Old Bot").closest("a")).toBeNull();
  });

  it("links the same agents for a manager", () => {
    renderTable(false);
    expect(agentLinks().map((el) => el.getAttribute("href")).sort()).toEqual(["/agents/long", "/agents/off", "/agents/open"]);
  });

  it("keeps a long agent name readable in full where the cell truncates it", () => {
    renderTable(true);
    // The name is plain text (the row opens the agent, #12070); its title carries it whole.
    expect(screen.getByTitle(LONG_NAME).textContent).toBe(LONG_NAME);
    expect(screen.getByTitle(LONG_NAME).closest("tr")?.querySelector('a[href="/agents/long"]')).not.toBeNull();
    // A plain-text name (not openable) carries it in its title too.
    expect(screen.getByTitle(`${LONG_NAME} (private)`).textContent).toBe(`${LONG_NAME} (private)`);
    expect(screen.getByTitle("Private Bot").textContent).toBe("Private Bot");
  });

  it("names a deleted agent with its chip and never shows a bare id", () => {
    renderTable(true);
    expect(screen.queryByText("ghost")).toBeNull();
    expect(screen.getByText("Deleted agent")).toBeTruthy();
    const chips = screen.getAllByTestId("top-agent-status").map((el) => el.textContent);
    // The test catalog renders ids: the chip's own status key.
    expect(chips.sort()).toEqual(["Deleted (status)", "Deleted (status)", "Disabled"]);
    const oldBotCell = screen.getByText("Old Bot").parentElement as HTMLElement;
    expect(within(oldBotCell).getByTestId("top-agent-status").textContent).toBe("Deleted (status)");
  });

  it("keeps every figure column sortable where the narrow card hides the secondary ones", () => {
    renderTable(true);
    // Hidden by container width only: still in the table, still sortable.
    for (const header of ["Avg cost / trace", "Avg latency", "Avg feedback"]) {
      expect(screen.getByRole("columnheader", { name: header }).getAttribute("aria-sort")).toBe("none");
    }
  });

  it("labels a company-managed owner with the short Company badge", () => {
    renderTable(true);
    const corpRow = screen.getByText("Corp Bot").closest("tr") as HTMLElement;
    expect(within(corpRow).getByText("Company")).toBeTruthy();
    expect(within(corpRow).queryByText("Company-managed")).toBeNull();
  });

  it("exports a deleted agent with no last known name as the screen names it, its id beside it", async () => {
    saved.blobs.length = 0;
    renderTable(true);
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(saved.blobs).toHaveLength(1);
    const lines = (await saved.blobs[0].text()).replace(/^\ufeff/, "").split(/\r?\n/);
    const ghost = lines.find((line) => line.split(",")[1] === "ghost");
    expect(ghost?.split(",")[0]).toBe("Deleted agent");
    // The figures the 1024 card does not draw are still exported.
    expect(lines[0]).toContain("Avg latency (ms)");
    expect(lines[0]).toContain("Avg feedback (0-1)");
  });

  it("caps a person owner and keeps the name and email whole in its title", () => {
    renderTable(true);
    // Plain text: the owner is never a link of its own in the row (#12070).
    const owner = screen.getByText("Ann Example").closest("[title]") as HTMLElement;
    expect(owner.getAttribute("title")).toBe("Ann Example\nann@example.com");
    expect(owner.closest("a")).toBeNull();
    expect(screen.queryAllByRole("link").some((el) => el.getAttribute("href") === "/users-and-groups/users/u1")).toBe(false);
  });

  it("reads its filters back from the URL under its own prefix", () => {
    // A reload or a shared link: the Status filter comes from `ba_`.
    renderTable(true, "/observability?breakdown=agents&ba_f_status=deleted");
    expect(screen.getByText("Old Bot")).toBeTruthy();
    expect(screen.queryByText("Shared Bot")).toBeNull();
    expect(screen.queryByText("Paused Bot")).toBeNull();
  });

  it("ignores the keys of the other analytics tables", () => {
    // The Top errors table (`te_`) and the users breakdown (`bu_`) share the page.
    renderTable(true, "/observability?te_q=Shared&bu_f_group=Analysts");
    expect(screen.getByText("Shared Bot")).toBeTruthy();
    expect(screen.getByText("Private Bot")).toBeTruthy();
  });

  it("keeps the By user Groups cell on one line and the narrow card free of a sideways scroll", () => {
    // UX round 1 on #10980, B1: two badges stacked and a group name wrapped to
    // three lines (92 px rows at 1080) in an 892 px table inside an 800 px card.
    const empty = { isLoading: false, isError: false };
    const groups = ["Lab OpenCRQ users", "Analysts", "Responders"];
    renderWithProviders(
      <MemoryRouter initialEntries={["/observability?breakdown=users"]}>
        <BreakdownCard
          breakdown="users"
          onBreakdownChange={() => {}}
          totalCost={1}
          costDataAvailable
          linkEntities
          agents={{ items: [], ...empty }}
          users={{
            data: {
              items: [
                {
                  user_id: "u1",
                  user_email: "ann@example.com",
                  user_name: "Ann Example",
                  is_active: true,
                  group_names: groups,
                  agent_count: 2,
                  trace_count: 3,
                  total_tokens: 10,
                  total_cost: 1,
                  avg_cost_per_trace: 0.3,
                },
              ],
              total_users: 1,
              other_users: null,
              unattributed: null,
            },
            ...empty,
          }}
          groups={empty}
          models={{ items: [], latency: [], ...empty }}
        />
      </MemoryRouter>,
    );
    // The cell goes through the shared one-line tag row, which folds into "+N".
    const cell = screen.getByTitle("Lab OpenCRQ users").closest("[data-tag-list]") as HTMLElement;
    expect(cell).toBeTruthy();
    for (const name of groups) expect(within(cell).getByTitle(name).textContent).toBe(name);
    // The secondary figures step aside in the 1080 card: still in the table, still sortable.
    for (const header of ["Agents", "Avg cost / trace"]) {
      expect(screen.getByRole("columnheader", { name: header }).getAttribute("aria-sort")).toBe("none");
    }
  });

  it("renders the By user table on production-shaped data: hundreds of users, a person in ninety groups, no groups, null costs", () => {
    // 16 Nov 2026: /analytics?breakdown=users blanked the whole page with
    // "Maximum update depth exceeded" from the Groups cell's TagList, which
    // folded one badge per synchronous pass for a person in ~90 groups.
    const sw = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.hasAttribute("data-tag-row") ? this.children.length * 70 : 0;
    });
    const cw = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.hasAttribute("data-tag-row") ? 160 : 0;
    });
    const empty = { isLoading: false, isError: false };
    const person = (index: number, groupNames: string[], cost: number | null = 1) => ({
      user_id: `u${index}`,
      user_email: `user${index}@example.com`,
      user_name: index % 7 === 0 ? null : `User ${index}`,
      is_active: index === 3 ? null : true,
      group_names: groupNames,
      agent_count: 2,
      trace_count: 3,
      total_tokens: 10,
      total_cost: cost as number,
      avg_cost_per_trace: cost as number,
    });
    const manyGroups = Array.from({ length: 90 }, (_, index) => (index % 3 === 0 ? `3f1c2a8e-0000-4000-8000-${String(index).padStart(12, "0")}` : `Group ${index} `));
    const items = [
      person(0, manyGroups, 87798),
      person(1, []),
      person(2, ["Uber business US ", "Analysts"], null),
      ...Array.from({ length: 197 }, (_, index) => person(index + 3, ["All Users", "Filigran", "Product - XTM One"])),
    ];
    renderWithProviders(
      <MemoryRouter initialEntries={["/observability?breakdown=users"]}>
        <BreakdownCard
          breakdown="users"
          onBreakdownChange={() => {}}
          totalCost={132143.9}
          costDataAvailable
          linkEntities
          agents={{ items: [], ...empty }}
          users={{
            data: {
              items,
              total_users: 235,
              other_users: { user_count: 35, trace_count: 40, total_tokens: 400, total_cost: 12, avg_cost_per_trace: 0.3 },
              unattributed: { trace_count: 5, total_tokens: 50, total_cost: 1, avg_cost_per_trace: 0.2 },
            },
            ...empty,
          }}
          groups={empty}
          models={{ items: [], latency: [], ...empty }}
        />
      </MemoryRouter>,
    );
    // The heaviest person leads, their groups folded into one "+N" that names them.
    expect(screen.getByText("user0@example.com")).toBeTruthy();
    expect(screen.getAllByText(/^\+\d+$/).length).toBeGreaterThan(0);
    expect(screen.getByText("Showing the 200 users with the highest cost out of 235; the others are counted together on the last page.")).toBeTruthy();
    sw.mockRestore();
    cw.mockRestore();
  });

  it("keeps the By model table inside the 1080 card, its secondary figures still sortable", () => {
    // UX round 2 on #10980: 10 columns drew 989 px in the 800 px card at 1080.
    const empty = { isLoading: false, isError: false };
    renderWithProviders(
      <MemoryRouter initialEntries={["/observability?breakdown=models"]}>
        <BreakdownCard
          breakdown="models"
          onBreakdownChange={() => {}}
          totalCost={1}
          costDataAvailable
          linkEntities
          agents={{ items: [], ...empty }}
          users={empty}
          groups={empty}
          models={{
            items: [
              {
                model: "gpt-test",
                trace_count: 3,
                input_tokens: 10,
                output_tokens: 5,
                input_cost: 0.4,
                output_cost: 0.6,
                total_cost: 1,
              },
            ],
            latency: [],
            ...empty,
          }}
        />
      </MemoryRouter>,
    );
    for (const header of ["Input Cost", "Output Cost", "P50"]) {
      expect(screen.getByRole("columnheader", { name: header }).getAttribute("aria-sort")).toBe("none");
    }
  });

  it("gives the chip its own status key, a status in Japanese, and leaves the confirmation alone", () => {
    const catalog = ja as Record<string, string>;
    expect((en as Record<string, string>)["Deleted (status)"]).toBe("Deleted");
    expect(catalog["Deleted (status)"]).toBe("削除済み");
    expect(catalog["Deleted"]).toBe("削除しました");
  });
});
