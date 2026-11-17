// Evaluation overview charts: the plot, its tooltip and its legend name and
// draw an agent/model pair the same way, in every chart.

import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { api } from "@/lib/api";
import { MODEL_INDEPENDENT } from "@/lib/constants";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationChartResponse, EvaluationCorrelationPoint, EvaluationRun } from "@/types";
import { OverviewTab } from "./EvaluationDetail";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// Recharts draws nothing at happy-dom's zero size. Each series stands in as an
// element named by its `name` prop, the text the Recharts tooltip shows for it.
vi.mock("recharts", async (importOriginal) => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  const Nothing = () => null;
  return {
    ...(await importOriginal<typeof import("recharts")>()),
    ResponsiveContainer: Pass,
    LineChart: Pass,
    ScatterChart: Pass,
    XAxis: Nothing,
    YAxis: Nothing,
    CartesianGrid: Nothing,
    Tooltip: Nothing,
    Label: Nothing,
    Line: ({ name, dataKey, stroke, strokeDasharray }: { name: string; dataKey: string; stroke: string; strokeDasharray?: string }) => (
      <span role="img" aria-label={name} data-key={dataKey} data-color={stroke} data-dash={strokeDasharray ?? ""} />
    ),
    Scatter: ({ name, data, fill }: { name: string; data: EvaluationCorrelationPoint[]; fill: string }) => (
      <span role="img" aria-label={name} data-key={data[0]?.series} data-color={fill} />
    ),
  };
});

const get = vi.mocked(api.get);

function run(id: string, agent: string, model: string): EvaluationRun {
  return {
    id,
    evaluation_id: "e-1",
    evaluation_name: "Eval",
    agent_id: `agent-${id}`,
    agent_name: agent,
    agent_slug: agent.toLowerCase(),
    agent_user_name: null,
    model_id: model,
    is_default_model: false,
    status: "completed",
    trigger_reason: "manual",
    score: 80,
    speed_score: null,
    overall_score: null,
    f1_score: null,
    quality_score: null,
    performance_score: null,
    quality_metric_keys: [],
    performance_metric_keys: [],
    quality_contributions: {},
    performance_contributions: {},
    quality_aggregation_excluded: [],
    performance_aggregation_excluded: [],
    quality_source: "judge",
    duration_seconds: 10,
    avg_sub_scores: {},
    completed_entries: 1,
    total_entries: 1,
    error: null,
    started_at: "2026-10-01T10:00:00Z",
    completed_at: "2026-10-01T10:05:00Z",
    created_at: "2026-10-01T10:00:00Z",
    can_replay: false,
  };
}

function corr(series: string, x: number, y: number): EvaluationCorrelationPoint {
  return { series, run_id: series, agent_id: series, model_id: series, x, y };
}

function chart(
  trends: Partial<EvaluationChartResponse["trends"]>,
  correlations: Partial<EvaluationChartResponse["correlations"]> = {},
): EvaluationChartResponse {
  return {
    range: "90d",
    trends: { quality: [], performance: [], judge: [], ...trends },
    correlations: { quality_performance: [], judge_performance: [], quality_judge: [], ...correlations },
    correlations_omitted: { quality_performance: 0, judge_performance: 0, quality_judge: 0 },
    runs_in_range: 1,
    total_runs: 1,
  };
}

function serve(runs: EvaluationRun[], data: EvaluationChartResponse) {
  get.mockImplementation(async (url: string) => {
    if (url.startsWith("/evaluations/e-1/runs")) return runs;
    if (url.startsWith("/evaluations/e-1/chart")) return data;
    throw new Error(`unexpected GET ${url}`);
  });
}

function renderOverview() {
  return renderWithProviders(
    <MemoryRouter>
      <OverviewTab evaluationId="e-1" />
    </MemoryRouter>,
  );
}

/** The swatch drawn next to a legend label. */
function swatch(label: HTMLElement) {
  const mark = label.previousElementSibling?.firstElementChild;
  return {
    color: mark?.getAttribute("stroke") ?? mark?.getAttribute("fill") ?? null,
    dash: mark?.getAttribute("stroke-dasharray") ?? "",
    kind: mark?.tagName.toLowerCase(),
  };
}

// Keys are `${agent}/${model}`. Model ids carry `/`, agent names may too.
const ALPHA = "Alpha/anthropic/claude-sonnet-5";
const BETA = "Team/Beta/gpt-5";
const GAMMA = `Gamma/${MODEL_INDEPENDENT}`;
const DELTA = "Delta/openai/gpt-4o"; // no run on the loaded page

const LABELS: Record<string, string> = {
  [ALPHA]: "Alpha · anthropic/claude-sonnet-5",
  [BETA]: "Team/Beta · gpt-5",
  [GAMMA]: "Gamma · Model-independent",
  [DELTA]: "Delta · openai/gpt-4o",
};

describe("overview chart series", () => {
  beforeEach(() => {
    get.mockReset();
    serve(
      [
        run("r-1", "Alpha", "anthropic/claude-sonnet-5"),
        run("r-2", "Team/Beta", "gpt-5"),
        run("r-3", "Gamma", MODEL_INDEPENDENT),
      ],
      chart(
        {
          // Only the last two pairs: positions in this chart differ from the evaluation's.
          quality: [{ triggered_at: "2026-10-01T10:00:00Z", trigger_reason: "manual", scores: { [GAMMA]: 70, [DELTA]: 50 } }],
          performance: [
            { triggered_at: "2026-10-01T10:00:00Z", trigger_reason: "manual", scores: { [ALPHA]: 60, [BETA]: 55, [GAMMA]: 40, [DELTA]: 30 } },
          ],
        },
        { quality_performance: [corr(GAMMA, 70, 40), corr(ALPHA, 65, 60)] },
      ),
    );
  });

  it("names each plotted series (the name its tooltip shows) the same as its legend label, never by its raw key", async () => {
    renderOverview();
    await screen.findAllByRole("img", { name: LABELS[GAMMA] });

    for (const [key, label] of Object.entries(LABELS)) {
      const plotted = screen.getAllByRole("img", { name: label });
      expect(plotted.every((el) => el.dataset.key === key)).toBe(true);
      expect(screen.getAllByText(label, { selector: "span" })).toHaveLength(plotted.length);
      expect(screen.queryByText(key)).toBeNull();
    }
    // Every plotted series is named through the same labels. The stubbed series carry a
    // colour; the page's info icons are images too, and are not series.
    const series = screen.getAllByRole("img").filter((el) => el.dataset.color !== undefined);
    const names = new Set(series.map((el) => el.getAttribute("aria-label")));
    expect([...names].sort()).toEqual(Object.values(LABELS).sort());
  });

  it("draws each legend swatch with the encoding its series is plotted with", async () => {
    renderOverview();
    await screen.findAllByRole("img", { name: LABELS[GAMMA] });

    for (const label of Object.values(LABELS)) {
      const plotted = screen.getAllByRole("img", { name: label });
      const legend = screen.getAllByText(label, { selector: "span" }).map(swatch);
      expect(legend).toEqual(
        plotted.map((el) =>
          el.dataset.dash === undefined
            ? { color: el.dataset.color, dash: "", kind: "path" }
            : { color: el.dataset.color, dash: el.dataset.dash, kind: "line" },
        ),
      );
    }
  });

  it("keeps one encoding per pair across charts, distinct between pairs", async () => {
    renderOverview();
    await screen.findAllByRole("img", { name: LABELS[GAMMA] });

    const colors = Object.values(LABELS).map((label) => {
      const seen = new Set(screen.getAllByRole("img", { name: label }).map((el) => el.dataset.color));
      expect(seen.size).toBe(1);
      return [...seen][0];
    });
    expect(new Set(colors).size).toBe(colors.length);
  });
});

describe("overview legend reuse note", () => {
  it("counts reuse from the evaluation-wide position, not the chart's own", async () => {
    // Eight pairs; the quality chart shows only the eighth, past the seven distinct styles.
    const runs = Array.from({ length: 8 }, (_, i) => run(`r-${i}`, `Agent${i}`, "gpt-5"));
    serve(
      runs,
      chart({ quality: [{ triggered_at: "2026-10-01T10:00:00Z", trigger_reason: "manual", scores: { "Agent7/gpt-5": 70 } }] }),
    );
    renderOverview();

    expect(await screen.findByText("1 series reuses an earlier line style: read them by colour and label")).toBeTruthy();
  });
});
