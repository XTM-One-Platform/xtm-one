// The "+N more" fold of the Data flow by keyboard (UX #9992 r2 B3): the button
// pressed unmounts, so focus moves to the toggle that replaces it instead of
// falling back to the page body. The view lays out on its box width, which
// happy-dom does not compute: the box is given a width here.

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";

import type { FleetFlow, FleetFlowEdge, FleetFlowNode } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useProviderCatalog: () => ({ data: [] }),
}));

const { FleetDataFlow } = await import("./FleetDataFlow");

const node = (id: string, lane: FleetFlowNode["lane"], kind: string): FleetFlowNode => ({
  id,
  lane,
  kind,
  name: id,
  icon: null,
  provider: null,
  integration_id: null,
  agent_id: lane === "agent" ? id : null,
  events: [],
  enabled: true,
  reason: null,
  visible: true,
});
const edge = (source: string, target: string, kind: FleetFlowEdge["kind"]): FleetFlowEdge => ({
  source,
  target,
  kind,
  assignment_id: kind === "trigger" ? `${source}-${target}` : null,
  assignment_name: kind === "trigger" ? `${source} to ${target}` : "",
  events: [],
  detail: "",
  filter: "",
  gate: "",
  type: null,
  enabled: true,
  reason: null,
});

// 2 sources, 2 agents, 20 outputs written: past twelve they fold into "+N more".
const OUTPUTS = Array.from({ length: 20 }, (_, i) => `Output ${i}`);
const FLOW: FleetFlow = {
  fleet_id: "f",
  nodes: [
    node("Source A", "source", "manual"),
    node("Source B", "source", "manual"),
    node("Agent A", "agent", "agent"),
    node("Agent B", "agent", "agent"),
    ...OUTPUTS.map((o) => node(o, "output", "integration")),
  ],
  edges: [
    edge("Source A", "Agent A", "trigger"),
    edge("Source B", "Agent B", "trigger"),
    ...OUTPUTS.map((o, i) => edge(i % 2 ? "Agent B" : "Agent A", o, "write")),
  ],
};

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 1408 });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterAll(() => {
  Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
  vi.unstubAllGlobals();
});

describe("FleetDataFlow output fold by keyboard (UX #9992 r2 B3)", () => {
  it("moves focus to Show less after Show all, and back to +N more after Show less", () => {
    renderWithProviders(<FleetDataFlow flow={FLOW} loading={false} error={false} onRetry={() => {}} onOpenAgent={() => {}} />);
    const more = screen.getByTestId("fleet-flow-more-outputs");
    more.focus();
    act(() => fireEvent.click(more));
    const less = screen.getByRole("button", { name: "Show less" });
    expect(document.activeElement).toBe(less);
    expect(screen.queryByTestId("fleet-flow-more-outputs")).toBeNull();

    act(() => fireEvent.click(less));
    expect(document.activeElement).toBe(screen.getByTestId("fleet-flow-more-outputs"));
    expect(screen.queryByRole("button", { name: "Show less" })).toBeNull();
  });
});
