/**
 * `shadow-overlay` stays on floating surfaces - `yarn test`.
 *
 * `--shadow-overlay` (index.css) is the lift of what floats over the page:
 * menus, popovers, the command palette, tooltips, dialogs, toolbars and pills
 * drawn over content. The token used to compute to `none`, so in-page
 * cards could carry it unseen; once it became a real shadow, 11 section cards
 * of Settings looked like popovers in light and not in dark (standing UI rule
 * of the CEO Office Developer skill, section 13).
 *
 * The allowlist below names every file that may use the class and how many
 * times. A new use is a floating surface added on purpose: add the file here
 * in the same PR. An in-page card, a node, a swatch or a thumb never gets it.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as animation-keyframes.test.ts.
const SRC = join(process.cwd(), "src") + sep;

/** File (from src/) -> the most uses of `shadow-overlay` it may hold, all floating surfaces. */
const FLOATING: Record<string, number> = {
  "components/arcade/ArcadeOverlay.tsx": 3,
  "components/chat/MentionMenu.tsx": 1,
  // The Health companion's bubble (#8261), floating over the page.
  "components/health/HealthCompanion.tsx": 1,
  "components/dashboard-grid/DashboardGrid.tsx": 3,
  // The typeahead list under the top bar search field (#11201).
  "components/layout/GlobalSearch.tsx": 1,
  "components/layout/Header.tsx": 2,
  "components/layout/ManualTriggerButton.tsx": 1,
  "components/presentation/SchemaFieldControls.tsx": 1,
  // The run's add button, the block bar, the text bar and the text bar's
  // slide-link picker, all floating over the slide.
  "components/presentation/SlideEditLayer.tsx": 4,
  "components/presentation/SlideJumpMenu.tsx": 1,
  "components/studio/StudioRail.tsx": 1,
  "components/ui/agent-flow-drawer.tsx": 1,
  "components/ui/agent-flow.tsx": 2,
  "components/ui/agent-focus-picker.tsx": 1,
  "components/ui/agent-graph-toolbar.tsx": 1,
  "components/ui/channel-picker.tsx": 1,
  "components/ui/dialog.tsx": 1,
  "components/ui/filter-pill.tsx": 1,
  "components/ui/handover-type-popover.tsx": 1,
  "components/ui/icon-picker.tsx": 1,
  // The one info popover (#9836 UX r1 B6): MetricInfo and "How this is computed" are built on it.
  "components/ui/info-popover.tsx": 1,
  "components/ui/principal-sharing-card.tsx": 1,
  "components/ui/slide-over.tsx": 1,
  "components/ui/tag-input.tsx": 1,
  "components/ui/tooltip.tsx": 1,
  "components/ui/variable-insert-button.tsx": 1,
  "components/video-studio/CinemaPlayer.tsx": 3,
  "pages/AiAdoption.tsx": 1,
  "pages/DatasetDetail.tsx": 1,
  "pages/EvaluationDetail.tsx": 2,
  "pages/Evaluations.tsx": 1,
  "pages/Intents.tsx": 1,
  "pages/PresentationViewer.tsx": 1,
  "pages/Profile.tsx": 1,
  "pages/UserDetail.tsx": 1,
  "pages/agent-detail/config/GeneralSection.tsx": 1,
  "pages/agent-detail/config/IntentsSection.tsx": 1,
  "pages/ai-adoption/StatusTab.tsx": 2,
  "pages/chat/ChatComposer.tsx": 1,
  "pages/chat/ChatMessages.tsx": 1,
  "pages/chat/ContextUsageIndicator.tsx": 1,
  "pages/chat/ConversationSidebar.tsx": 1,
  "pages/flow-builder/FlowCanvas.tsx": 1,
  "pages/flow-builder/QuickAddMenu.tsx": 1,
  "pages/flow-builder/SelectionToolbar.tsx": 1,
  "pages/flow-builder/VariableInput.tsx": 1,
  "pages/flows/MapTab.tsx": 1,
  "pages/integration-detail/lmstudio/LMStudioModelsTab.tsx": 1,
  "pages/integration-detail/openrouter/OpenRouterModelsTab.tsx": 1,
  "pages/public/components.tsx": 2,
  "pages/settings/MCPEndpointSection.tsx": 1,
  "pages/settings/ModelPricingSection.tsx": 1,
};

function* tsxFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* tsxFiles(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

function uses(): Map<string, number> {
  const out = new Map<string, number>();
  for (const full of tsxFiles(SRC)) {
    const count = readFileSync(full, "utf8").match(/\bshadow-overlay\b/g)?.length ?? 0;
    if (count > 0) out.set(full.slice(SRC.length).split(sep).join("/"), count);
  }
  return out;
}

test("shadow-overlay is used on floating surfaces only", () => {
  const found = uses();
  expect(found.size).toBeGreaterThan(0);
  const offenders = [...found]
    .filter(([path, count]) => count > (FLOATING[path] ?? 0))
    .map(([path, count]) => `${path}: ${count} use(s), ${FLOATING[path] ?? 0} allowed`);
  expect(offenders, "shadow-overlay on an in-page surface: drop it, or add the floating surface to FLOATING").toEqual([]);
});

/** A panel (`bg-elevation-default`) with a plain, non-variant `shadow-[0_0_<N>px ...]` glow literal. */
const PANEL_GLOW_LITERAL = /(?<![\w:-])shadow-\[0_0_[1-9]\d*px/;

/** Index just past the string literal opening at `start` (', " or `), or the end of the source. */
function skipString(src: string, start: number): number {
  const quote = src[start];
  for (let i = start + 1; i < src.length; i++) {
    if (src[i] === "\\") i++;
    else if (src[i] === quote) return i + 1;
  }
  return src.length;
}

/** Index just past the bracket closing the one opening at `start` (`(` or `{`), string literals skipped. */
function skipBalanced(src: string, start: number): number {
  let depth = 0;
  for (let i = start; i < src.length; ) {
    const c = src[i];
    if (c === '"' || c === "'" || c === "`") {
      i = skipString(src, i);
      continue;
    }
    if (c === "(" || c === "{") depth++;
    else if (c === ")" || c === "}") {
      depth--;
      if (depth === 0) return i + 1;
    }
    i++;
  }
  return src.length;
}

/**
 * Every class expression of a source, whatever its line breaks: a `className="..."` string,
 * a `className={...}` expression and a `cn(...)` / `clsx(...)` call, each with the line it
 * starts on. Matching per expression, not per line, keeps a panel whose classes are split
 * across a multi-line `cn()` inside the guard below.
 */
function classExpressions(src: string): { line: number; text: string }[] {
  const out: { line: number; text: string }[] = [];
  const opener = /\bclassName\s*=\s*(?=["'{`])|\b(?:cn|clsx)\s*(?=\()/g;
  for (let m = opener.exec(src); m; m = opener.exec(src)) {
    const start = m.index + m[0].length;
    const end = src[start] === "(" || src[start] === "{" ? skipBalanced(src, start) : skipString(src, start);
    out.push({ line: src.slice(0, m.index).split("\n").length, text: src.slice(start, end) });
  }
  return out;
}

/** The class expressions of a source that paint a panel with a hand-written glow literal. */
function glowLiteralPanels(src: string): number[] {
  // `className={cn(...)}` is read twice (the attribute and the call): one line per panel.
  const lines = classExpressions(src)
    .filter(({ text }) => /\bbg-elevation-default\b/.test(text) && PANEL_GLOW_LITERAL.test(text))
    .map(({ line }) => line);
  return [...new Set(lines)];
}

test("the glow-literal guard reads a class expression across its line breaks", () => {
  const multiLine = [
    "<div",
    "  className={cn(",
    '    "rounded-sm bg-elevation-default",',
    '    open && "ring-1",',
    '    "shadow-[0_0_40px_red]",',
    "  )}",
    "/>",
  ].join("\n");
  expect(glowLiteralPanels(multiLine)).toEqual([2]);
  expect(glowLiteralPanels('<div className="bg-elevation-default shadow-[0_0_24px_red]" />')).toEqual([1]);
  expect(
    glowLiteralPanels("const c = cn(\n  `bg-elevation-default ${x}`,\n  'shadow-[0_0_12px_red]',\n);"),
  ).toEqual([1]);
  // Out of scope, as documented in dev-docs/reference/frontend-design-system.md ("Shadows").
  expect(glowLiteralPanels('<div className={cn("bg-elevation-default", "hover:shadow-[0_0_24px_red]")} />')).toEqual([]);
  expect(glowLiteralPanels('<span className="rounded-full shadow-[0_0_6px_green]" />')).toEqual([]);
  expect(glowLiteralPanels('<div className={cn("bg-elevation-default", "shadow-glow-accent")} />')).toEqual([]);
  // Two sibling elements: the panel and the glow live in different expressions.
  expect(
    glowLiteralPanels('<div className="bg-elevation-default">\n  <i className="shadow-[0_0_6px_red]" />\n</div>'),
  ).toEqual([]);
});

test("a floating panel glows through a named token, never a hand-written literal", () => {
  const offenders: string[] = [];
  for (const full of tsxFiles(SRC)) {
    for (const line of glowLiteralPanels(readFileSync(full, "utf8"))) {
      offenders.push(`${full.slice(SRC.length).split(sep).join("/")}:${line}`);
    }
  }
  expect(offenders, "use shadow-glow-accent / shadow-glow-accent-strong (index.css) or shadow-overlay").toEqual([]);
});

test("the in-page cards never carry shadow-overlay again", () => {
  const found = uses();
  for (const path of [
    "pages/Settings.tsx",
    "pages/Dashboard.tsx",
    "pages/flow-builder/FlowNode.tsx",
    "pages/new-agent-wizard/WizardCanvas.tsx",
    "pages/xtm-one/ctem/ctem-viz.tsx",
    "pages/xtm-one/ctem/VerticalSwitcher.tsx",
    "components/presentation/TemplateGeneratorDialog.tsx",
    "components/video-studio/editor/PreviewStage.tsx",
  ]) {
    expect(found.get(path) ?? 0, path).toBe(0);
  }
});
