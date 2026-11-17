// A stored answer's chart fence on the chat panel: a block the chat draws
// becomes its chart, anything else stays the code it is (`null`).

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { CHART_CODE_BLOCK_RENDERERS, chartCodeBlockRenderer } from "@/components/chat-panel/chart-code-block";
import { CHART_FENCE_LANGUAGE } from "@/components/chat/chat-chart";

vi.mock("@/components/chat/ChatChart", () => ({
  default: ({ block }: { block: { title?: string } }) => <figure data-testid="chart">{block.title}</figure>,
}));

const BLOCK = {
  type: "chart",
  kind: "line",
  categories: ["2026-09-28", "2026-10-05"],
  series: [{ name: "model-one", values: [1.5, 2.25] }],
  title: "Cost",
};

describe("chart code blocks", () => {
  it("draws a chart block of the chat's fence language", async () => {
    expect(CHART_CODE_BLOCK_RENDERERS[CHART_FENCE_LANGUAGE]).toBe(chartCodeBlockRenderer);
    render(<>{chartCodeBlockRenderer(JSON.stringify(BLOCK))}</>);
    expect((await screen.findByTestId("chart")).textContent).toBe("Cost");
  });

  it("leaves a block that is not a chart to the panel as code", () => {
    expect(chartCodeBlockRenderer("not a chart")).toBeNull();
    expect(chartCodeBlockRenderer(JSON.stringify({ type: "table" }))).toBeNull();
  });
});
