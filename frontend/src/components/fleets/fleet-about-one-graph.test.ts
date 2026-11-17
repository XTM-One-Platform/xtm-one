/**
 * One graph component on the fleet About page - `yarn test`.
 *
 * The inline flow of the About view (#9539 slice S3) is the platform's own
 * `AgentFlowGraph` (components/ui/agent-flow.tsx), passed in by the fleet page
 * with the Graph view's data. No file under components/fleets/ draws a graph
 * of its own: no React Flow import, no hand-drawn SVG flow (UX design verdict
 * of 7 Nov 2026, blocker B1).
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const DIR = join(process.cwd(), "src", "components", "fleets");

test("no file under components/fleets imports a graph library", () => {
  const offenders = readdirSync(DIR)
    .filter((name) => /\.(ts|tsx)$/.test(name) && !name.endsWith(".test.ts") && !name.endsWith(".test.tsx"))
    .filter((name) => /from\s+["'](@xyflow\/react|reactflow|d3-force|mermaid)["']/.test(readFileSync(join(DIR, name), "utf8")));
  assert.deepEqual(offenders, []);
});

test("an undocumented fleet's About still draws its flow under the Generate panel (#9647 UX round 2, B3; #10316)", () => {
  const view = readFileSync(join(DIR, "FleetAboutView.tsx"), "utf8");
  const branch = view.slice(view.indexOf("if (sections.length === 0) {"), view.indexOf('data-testid="fleet-about"'));
  const empty = branch.indexOf('t_i18n("No documentation yet")');
  // The computed facts go through the showcase (which draws the flow), else the flow alone.
  assert.ok(empty > 0, "the undocumented branch keeps its Generate panel");
  assert.match(branch, /doc\.structured \? \(\s*<FleetAboutShowcase[\s\S]*?flow=\{flow\}[\s\S]*?\) : \(\s*flow && <FleetAboutFlow flow=\{flow\}/);
  assert.equal(/<EmptyState/.test(branch), false);
});

test("the About showcase takes its flow from the page, it never builds one", () => {
  const showcase = readFileSync(join(DIR, "FleetAboutShowcase.tsx"), "utf8");
  assert.equal(/<AgentFlowGraph|<svg|from\s+["']@\/components\/ui\/agent-flow["']/.test(showcase), false);
  const page = readFileSync(join(process.cwd(), "src", "pages", "flows", "FleetPage.tsx"), "utf8");
  // A force layout computed once and frozen (#10123).
  assert.match(page, /flow=\{[\s\S]*?<AgentFlowGraph[\s\S]*?staticView[\s\S]*?layout="force"/);
});

// #9757: the About flow is a fixed mini graph; the page scrolls over it.
const GRAPH = readFileSync(join(process.cwd(), "src", "components", "ui", "agent-flow.tsx"), "utf8");

test("the About flow is a static view that opens the full graph", () => {
  const page = readFileSync(join(process.cwd(), "src", "pages", "flows", "FleetPage.tsx"), "utf8");
  const about = page.slice(page.indexOf("<FleetAboutView"), page.indexOf("</FleetAboutView>") > 0 ? page.indexOf("</FleetAboutView>") : page.indexOf(") : view === \"members\""));
  assert.match(about, /<AgentFlowGraph[\s\S]*?\bstaticView\b[\s\S]*?onStaticOpen=\{openFullGraph\}/);
  // No toolbar, no details drawer, no agent navigation on the mini graph.
  assert.equal(/<AgentGraphToolbar|onShowDetails=|onNavigateToAgent=/.test(about), false);
  // Every edge of the fleet graph is drawn, rewired to the groups the mini
  // graph folds another fleet's agents into (#9760 UX round 3, B1).
  assert.match(about, /agents=\{mini\.agents\}[\s\S]*?edgeIds=\{mini\.edgeIds\}/);
  // The mini graph folds the fleet's ghosts alone; A2A partners are drawn on
  // the full graph only (#12337).
  assert.match(page, /fleetMiniGraph\(fleetAgents, fleetGhosts, edgeIds, dashedEdgeIds,/);
});

test("a static view never takes the wheel, never pans and draws no zoom rail", () => {
  // The wheel listener is not attached: the wheel scrolls the page.
  assert.match(GRAPH, /if \(!el \|\| staticView\) return;\s*const handleWheel/);
  assert.match(GRAPH, /if \(e\.button !== 0 \|\| staticView\) return;/);
  assert.match(GRAPH, /\{!staticView && \(\s*<div ref=\{zoomRailRef\}/);
  // A static container keeps neither the overscroll lock nor the touch lock.
  const style = GRAPH.slice(GRAPH.indexOf("data-static-view="), GRAPH.indexOf("onPointerDown={handlePointerDown}"));
  const staticStyle = style.slice(style.indexOf("staticView\n"), style.indexOf("overscrollBehavior"));
  assert.match(staticStyle, /\{ cursor: onStaticOpen \? "pointer" : "default" \}/);
  assert.equal(/touchAction/.test(staticStyle), false);
});

test("the full graph opened from the About view focuses the agent alone (#9760 UX round 1, B4)", () => {
  // The graph adds the handover neighbours itself; a page that passed them too
  // focused two hops, so next to a hub nothing was dimmed.
  const page = readFileSync(join(process.cwd(), "src", "pages", "flows", "FleetPage.tsx"), "utf8");
  assert.match(page, /focusAgentId \? new Set\(\[focusAgentId\]\) : null/);
});

test("UX r1 of #10147: setup row when not set up, one status source, one control height", () => {
  const showcase = readFileSync(join(DIR, "FleetAboutShowcase.tsx"), "utf8");
  // B1: a fleet not set up shows the compact setup row, not the KPI tiles.
  assert.match(showcase, /status === "not_set_up" \? \([\s\S]*?data-testid="about-setup-row"[\s\S]*?\) : \([\s\S]*?data-testid="about-kpis"/);
  // B2: the hero chip reads the header pill's source first.
  assert.match(showcase, /const status = fleetOptIn\(health\?\.automations\)/);
  // B4: Expand and Open the full graph share one 32 px height.
  assert.match(showcase, /className="h-8 gap-1\.5" onClick=\{onOpenGraph\}/);
  assert.match(showcase, /className="hidden h-8 gap-1\.5 xl:inline-flex"/);
});

test("#10333 UX r2 B1: unfolded steps never stretch the diagram card, which keeps its own height", () => {
  const showcase = readFileSync(join(DIR, "FleetAboutShowcase.tsx"), "utf8");
  assert.match(showcase, /<FleetAboutSteps structured=\{s\} onExpandedChange=\{setStepsExpanded\} \/>/);
  // r3 B1: the sticky card clears the shell's sticky header (shared offset of FleetPage and section-index), never top-4.
  assert.match(
    showcase,
    /className=\{stepsExpanded \? "xl:sticky xl:top-\[calc\(var\(--fds-header-height,68px\)\+1rem\)\] xl:self-start" : undefined\}/,
  );
  assert.doesNotMatch(showcase, /xl:top-4/);
  assert.match(showcase, /<Section\s+id="about-diagram"\s+className=\{className\}/);
});

test("one way to the Graph tab: the Flow header link replaces the About button", () => {
  const view = readFileSync(join(DIR, "FleetAboutView.tsx"), "utf8");
  assert.match(view, /const openGraph = !flow && \(\s*<Button[\s\S]*?Open the graph/);
  const showcase = readFileSync(join(DIR, "FleetAboutShowcase.tsx"), "utf8");
  assert.match(showcase, /data-testid="about-open-full-graph"/);
});
