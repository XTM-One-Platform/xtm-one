/**
 * Gate: every Recharts tooltip of the frontend wears the one shared look,
 * CHART_TOOLTIP_PROPS. A file-local style object or an inline `contentStyle={{` brings back a
 * second tooltip look (and a tooltip tall enough to flash a chart scroller). A tooltip that draws its
 * whole box through its own `content` renderer (the presentation deck charts) has no Recharts chrome
 * to share and is left to that renderer.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { CHART_TOOLTIP_PROPS } from "./chart-tooltip";

const SRC = resolve(process.cwd(), "src");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

/** The files that import the Recharts `Tooltip`, the only ones a chart tooltip can live in. */
const sources = sourceFiles(SRC)
  .map((path) => ({ name: relative(SRC, path), text: readFileSync(path, "utf8") }))
  .filter(({ text }) => /import\s*\{[^}]*\bTooltip\b[^}]*\}\s*from\s*["']recharts["']/.test(text));

describe("chart tooltips", () => {
  it("reads every Recharts tooltip source of the frontend", () => {
    const names = sources.map(({ name }) => name.split("\\").join("/"));
    for (const expected of [
      "pages/analytics/UsageOverTimeCard.tsx",
      "pages/analytics/LatencyCard.tsx",
      "components/chat/ChatChart.tsx",
      "components/ui/charts/cost-over-time-chart.tsx",
      "pages/profile/agent-accounts/AgentAccountOverview.tsx",
      "pages/EvaluationDetail.tsx",
    ]) {
      expect(names).toContain(expected);
    }
  });

  it.each(sources)("$name spreads the shared props on every <Tooltip>", ({ text }) => {
    // A tooltip's props hold arrow functions, so the element runs to its self-closing `/>`.
    const tooltips = (text.match(/<Tooltip\b[\s\S]*?\/>/g) ?? []).filter((tooltip) => !/\bcontent=\{/.test(tooltip));
    for (const tooltip of tooltips) expect(tooltip).toContain("{...CHART_TOOLTIP_PROPS}");
    expect(text).not.toMatch(/contentStyle=\{\{/);
    expect(text).not.toMatch(/const \w*TOOLTIP\w*\s*=/);
  });

  it("keeps the tooltip compact and still", () => {
    expect(CHART_TOOLTIP_PROPS.isAnimationActive).toBe(false);
    expect(CHART_TOOLTIP_PROPS.contentStyle.fontSize).toBe(12);
  });
});
