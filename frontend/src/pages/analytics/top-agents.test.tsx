// The "Top agents" rows: a link where the server allows it, inert
// elsewhere, a deleted agent named and never opened, and no click filters.

import { describe, expect, it } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { Bot } from "lucide-react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { RankingCard } from "@/pages/analytics/RankingCard";
import { TopAgentStatusBadge, topAgentHref, topAgentLabel } from "@/pages/analytics/top-agents";
import type { TraceAnalyticsCostByAgentItem } from "@/types";

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

const ROWS = [
  row({ agent_id: "open", agent_name: "Shared Bot", can_open: true, agent_url: "/agents/open", total_cost: 3 }),
  row({ agent_id: "shut", agent_name: "Private Bot", total_cost: 2 }),
  row({ agent_id: "gone", agent_name: "Old Bot", agent_status: "deleted", can_open: true, agent_url: "/agents/gone", total_cost: 1 }),
];

function Where() {
  const location = useLocation();
  return <p data-testid="where">{`${location.pathname}${location.search}`}</p>;
}

function renderCard() {
  const t = (id: string) => id;
  return renderWithProviders(
    <MemoryRouter initialEntries={["/observability?period=7d"]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <RankingCard
                title="Top agents"
                icon={Bot}
                items={ROWS}
                itemKey={(item) => item.agent_id}
                itemLabel={(item) => topAgentLabel(item, t)}
                itemTitle={(item) => topAgentLabel(item, t)}
                metrics={[{ key: "cost", label: "Cost", value: (item) => item.total_cost, format: String, additive: true }]}
                costDataAvailable
                isLoading={false}
                emptyLabel="none"
                itemHref={topAgentHref}
                itemBadge={(item) => <TopAgentStatusBadge item={item} />}
              />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Top agents rows", () => {
  it("links only the agents the server lets the caller open, never a deleted one", () => {
    expect(topAgentHref(ROWS[0])).toBe("/agents/open");
    expect(topAgentHref(ROWS[1])).toBeUndefined();
    expect(topAgentHref(ROWS[2])).toBeUndefined();
  });

  it("names a deleted agent instead of showing its id", () => {
    const t = (id: string) => id;
    expect(topAgentLabel(row({ agent_name: null, agent_status: "deleted" }), t)).toBe("Deleted agent");
    expect(topAgentLabel(row({ agent_name: "Old Bot", agent_status: "deleted" }), t)).toBe("Old Bot");
  });

  it("renders an openable row as a link and the others inert, with a deleted chip", () => {
    renderCard();
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("/agents/open");
    expect(screen.queryAllByRole("button").filter((el) => el.textContent?.includes("Bot"))).toHaveLength(0);

    const inert = screen.getByText("Private Bot").closest("li")?.firstElementChild;
    expect(inert?.tagName).toBe("DIV");
    expect(inert?.getAttribute("tabindex")).toBeNull();
    // Its own status key; the test catalog renders ids.
    expect(screen.getByTestId("top-agent-status").textContent).toBe("Deleted (status)");
  });

  it("carries the full name on the truncated label of every row", () => {
    renderCard();
    for (const name of ["Shared Bot", "Private Bot", "Old Bot"]) {
      expect(screen.getByTitle(name).textContent).toBe(name);
    }
    // The openable row: the title sits on the label inside the link.
    expect(screen.getByRole("link").querySelector("[title='Shared Bot']")).toBeTruthy();
  });

  it("navigates on click and never touches the dashboard filters", () => {
    renderCard();
    fireEvent.click(screen.getByText("Private Bot"));
    fireEvent.click(screen.getByText("Old Bot"));
    expect(screen.getByTestId("where").textContent).toBe("/observability?period=7d");

    fireEvent.click(screen.getByRole("link"));
    expect(screen.getByTestId("where").textContent).toBe("/agents/open");
  });
});
