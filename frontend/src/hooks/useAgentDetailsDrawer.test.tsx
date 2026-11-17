// The agent definition drawer of the agent graphs: one hook, used by
// the Agentic Flow map and by a fleet's Graph tab, never a copy per page.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, renderHook, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentFlowDrawer } from "@/components/ui/agent-flow-drawer";
import { renderWithProviders } from "@/testing/providers";
import type { Agent, Assignment } from "@/types";

vi.mock("@/hooks/api/useAssignmentApi", () => ({
  useAssignments: () => ({ data: [] }),
  useAllAssignmentRuns: () => ({ data: [] }),
}));

import { useAgentDetailsDrawer } from "./useAgentDetailsDrawer";

const PAGES_DRAWING_THE_GRAPH = ["src/pages/flows/MapTab.tsx", "src/pages/flows/FleetPage.tsx"];

function source(path: string): string {
  return readFileSync(resolve(process.cwd(), path), "utf8");
}

const AGENTS = [
  { id: "a1", name: "Analyst", icon: "bot", handover_agent_ids: [], handover_types: {} },
  { id: "a2", name: "Developer", icon: "bot", handover_agent_ids: [], handover_types: {} },
] as unknown as Agent[];

describe("useAgentDetailsDrawer gate", () => {
  it.each(PAGES_DRAWING_THE_GRAPH)("%s opens the shared drawer from the node's \"i\"", (path) => {
    const text = source(path);
    expect(text).toContain("useAgentDetailsDrawer(");
    expect(text).toMatch(/onShowDetails=\{/);
    expect(text).toMatch(/selectedNodeId=\{details\.selectedId\}/);
    expect(text).toContain("{details.element}");
    // The drawer is mounted by the hook only: no second copy on a page.
    expect(text).not.toContain("<AgentFlowDrawer");
  });
});

describe("useAgentDetailsDrawer", () => {
  const options = { agents: AGENTS, activeAgentIds: new Set<string>(), onOpenAgent: () => {} };

  it("opens on an agent the reader sees and selects its node", () => {
    const { result } = renderHook(() => useAgentDetailsDrawer(options));
    expect(result.current.selectedId).toBeNull();
    act(() => result.current.show("a2"));
    expect(result.current.selectedId).toBe("a2");
    act(() => result.current.close());
    expect(result.current.selectedId).toBeNull();
  });

  it("stays closed on an agent the reader does not see", () => {
    const { result } = renderHook(() => useAgentDetailsDrawer(options));
    act(() => result.current.show("unknown"));
    expect(result.current.selectedId).toBeNull();
  });

  it("renders the drawer over a backdrop that closes it on a click", () => {
    const { result } = renderHook(() => useAgentDetailsDrawer(options));
    act(() => result.current.show("a1"));
    renderWithProviders(<>{result.current.element}</>);
    expect(screen.getByRole("dialog", { name: "Analyst details" })).toBeTruthy();
    act(() => screen.getByTestId("agent-flow-drawer-backdrop").click());
    expect(result.current.selectedId).toBeNull();
  });

  it("shows the full name on the truncated title and closes from the header row", () => {
    const { result } = renderHook(() => useAgentDetailsDrawer(options));
    act(() => result.current.show("a1"));
    renderWithProviders(<>{result.current.element}</>);
    const title = screen.getByRole("heading", { name: "Analyst" });
    expect(title.getAttribute("title")).toBe("Analyst");
    const close = screen.getByRole("button", { name: "Close" });
    // In the header flex row beside the title.
    expect(close.parentElement).toBe(title.parentElement?.parentElement);
    act(() => close.click());
    expect(result.current.selectedId).toBeNull();
  });
});

describe("AgentFlowDrawer body strings", () => {
  const agent = { ...AGENTS[0], enabled: true } as Agent;
  const one = [{ id: "s1", agent_id: "a1", name: "Daily brief", trigger_type: "manual", enabled: true }] as unknown as Assignment[];

  function drawer(assignments: Assignment[]) {
    return (
      <AgentFlowDrawer
        agent={agent}
        assignments={assignments}
        activeAgentIds={new Set<string>()}
        agentNameMap={new Map()}
        open
        onClose={() => {}}
        onOpenAgent={() => {}}
      />
    );
  }

  it("counts one assignment in the singular", () => {
    renderWithProviders(drawer(one));
    expect(screen.getByTestId("agent-flow-drawer-assignment-count").textContent).toBe("1 assignment");
  });

  it("reads the drawer body in French", () => {
    renderWithProviders(drawer([]), { locale: "fr" });
    expect(screen.getByTestId("agent-flow-drawer-assignment-count").textContent).toBe("0 assignation");
    expect(screen.getByText("Activé")).toBeTruthy();
    expect(screen.getByText("Assignations")).toBeTruthy();
    expect(screen.getByText("Aucune assignation configurée.")).toBeTruthy();
    expect(screen.getByText("Exécutions récentes")).toBeTruthy();
    expect(screen.getByText("Aucune exécution pour l'instant.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Ouvrir l'agent" })).toBeTruthy();
  });

  it("reads an assignment's trigger in French", () => {
    renderWithProviders(drawer(one), { locale: "fr" });
    expect(screen.getByText("Manuel")).toBeTruthy();
    expect(screen.queryByText("Manual")).toBeNull();
  });
});
