/**
 * Chips and badges take the design-system chip radius, never a full pill - `yarn test`.
 *
 * `@filigran/design-system` `Chip` draws `rounded-sm` (the 4 px step of the
 * radius scale) with a 1 px edge and `px-2`. The shared `Badge`
 * (components/ui/badge.tsx), and `ToneBadge` and `DiffBadge` on top of it,
 * follow that shape (#1333). This gate keeps it that way:
 *
 * 1. the shared chip components carry `rounded-sm` and no `rounded-full`;
 * 2. no `<Badge>` or `<ToneBadge>` overrides the radius at a call site;
 * 3. no new hand-made chip is drawn as a pill: a class string that carries
 *    `rounded-full`, an inline padding (`px-1` to `px-4`) and a type size
 *    (`content-*`, `text-xs`, `text-[Npx]`) is a chip, unless it is a fixed
 *    square (`h-N w-N`, `size-N`: an avatar, a dot, a round button) or a
 *    numeric counter (`min-w-*`). The chips not moved yet are listed in
 *    PENDING_PILL_CHIPS with their count per file; a slice that moves one
 *    lowers its count, a new pill fails.
 *
 * 4. a `<Badge>` holding only a count is a `CountBadge` (round counter).
 *
 * Avatars, status dots, progress bars, toggles, spinners, round icon buttons
 * and counters are not chips and stay round.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as focusable-badge-trigger.test.ts.
const SRC = join(process.cwd(), "src") + sep;

/** One class string (between quotes or backticks) on one line. */
const CLASS_STRING = /["'`]([^"'`\n]*)["'`]/g;

/** A pill-shaped chip: full radius, inline padding and a type size in one class string. */
function isPillChip(classes: string): boolean {
  if (!/(?:^|\s)rounded-full(?=\s|$)/.test(classes)) return false;
  if (!/(?:^|\s)px-(?:1|1\.5|2|2\.5|3|3\.5|4)(?=\s|$)/.test(classes)) return false;
  if (!/(?:^|\s)(?:content-[a-z-]+|text-xs|text-\[\d+px\])(?=\s|$)/.test(classes)) return false;
  // A fixed square (avatar, dot, round button) or a numeric counter is not a chip.
  if (/(?:^|\s)(?:h-\[?[\d.]+(?:px)?\]?\s+w-\[?[\d.]+(?:px)?\]?|size-[\d.]+|min-w-[^\s]+)(?=\s|$)/.test(classes)) return false;
  return true;
}

/**
 * Hand-made pill chips still to move onto the shared Badge or the DS radius,
 * by file, with their count (#1333 slices 2 and later). Never add a row: a
 * new chip uses Badge or ToneBadge.
 */
const PENDING_PILL_CHIPS: Record<string, number> = {
  "components/arcade/ArcadeOverlay.tsx": 5,
  "components/custom-dashboard/widgets.tsx": 1,
  "components/evaluation/bundle-review/DiffView.tsx": 1,
  "components/evaluation/bundle-review/ReviewInspector.tsx": 1,
  "components/evaluation/bundle-review/ReviewRail.tsx": 1,
  "components/fleets/FleetDataFlow.tsx": 1,
  "components/investigation/GoalPlanView.tsx": 1,
  "components/investigation/pack-builder/PackBuilder.tsx": 1,
  "components/presentation/TemplateGeneratorDialog.tsx": 1,
  "components/traces/span-inspector.tsx": 1,
  "components/traces/span-node.tsx": 1,
  "components/ui/agent-flow.tsx": 1,
  "components/ui/agent-graph-toolbar.tsx": 1,
  "components/ui/filter-pill.tsx": 1,
  "components/video-studio/editor/PreviewStage.tsx": 1,
  "components/video-studio/editor/SceneInspector.tsx": 1,
  "pages/Dashboard.tsx": 2,
  "pages/DatasetDetail.tsx": 1,
  "pages/Desktop.tsx": 1,
  "pages/EvaluationRunCompare.tsx": 1,
  "pages/EvaluationRunDetail.tsx": 1,
  "pages/KnowledgeBases.tsx": 1,
  "pages/PresentationViewer.tsx": 1,
  "pages/Profile.tsx": 1,
  "pages/VideoStudio.tsx": 1,
  "pages/agent-detail/MemoryTab.tsx": 2,
  "pages/agent-detail/config/ChannelsSection.tsx": 1,
  "pages/chat/AwaitedWorkBanner.tsx": 1,
  "pages/chat/RunnerActivity.tsx": 1,
  "pages/new-agent-wizard/WizardAssistant.tsx": 1,
  "pages/new-agent-wizard/WizardCanvas.tsx": 1,
  "pages/profile/agent-accounts/AgentAccountRules.tsx": 3,
  "pages/public/PublicAgentDetail.tsx": 1,
  "pages/settings/ChannelManagement.tsx": 1,
  "pages/settings/ScannedToolsField.tsx": 1,
  "pages/settings/SecurityAuditSection.tsx": 1,
  "pages/xtm-one/CtemCommandCenter.tsx": 2,
  "pages/xtm-one/ExposureBriefing.tsx": 1,
  "pages/xtm-one/ctem/ctem-viz.tsx": 1,
};

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : sourceFiles(path);
    return /\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name) ? [path] : [];
  });
}

function pillChipCounts(): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const path of sourceFiles(SRC)) {
    const text = readFileSync(path, "utf8");
    const hits = [...text.matchAll(CLASS_STRING)].filter((m) => isPillChip(m[1])).length;
    if (hits > 0) counts[path.slice(SRC.length).split(sep).join("/")] = hits;
  }
  return counts;
}

test("the shared chip components draw the design-system radius", () => {
  for (const file of ["components/ui/badge.tsx", "components/ui/diff-badge.tsx"]) {
    const text = readFileSync(join(SRC, file), "utf8");
    const code = text.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
    expect(code, file).toMatch(/\brounded-sm\b/);
    expect(code, file).not.toMatch(/\brounded-full\b/);
  }
});

test("no Badge or ToneBadge overrides the chip radius at a call site", () => {
  const OVERRIDE = /<(?:Badge|ToneBadge)\b[^<>]*?\brounded-(?:full|none|xs|md|lg|xl|2xl|3xl|\[[^\]]*\])/g;
  const offenders = sourceFiles(SRC).flatMap((path) => {
    const text = readFileSync(path, "utf8");
    return [...text.matchAll(OVERRIDE)].map((hit) => `${path.slice(SRC.length)}:${text.slice(0, hit.index).split("\n").length}`);
  });
  expect(offenders).toEqual([]);
});

/**
 * A `<Badge>` whose only child is a count (a number, or an expression ending in
 * `.length`, `.size`, `count` or `total`) is a counter, not a chip: it is drawn
 * with `CountBadge` (a 20 px circle, the design-system standalone count), so a
 * count never takes the square chip shape (#1333, UX round 1).
 */
const COUNT_ONLY_BADGE = /<Badge\b[^<>]*>\s*(?:\d+|\{[^{}]*(?:\.length|\.size|[cC]ount|[tT]otal\w*)\s*\})\s*<\/Badge>/g;

test("a count is drawn with CountBadge, never a chip-shaped Badge", () => {
  const offenders = sourceFiles(SRC).flatMap((path) => {
    const text = readFileSync(path, "utf8");
    return [...text.matchAll(COUNT_ONLY_BADGE)].map((hit) => `${path.slice(SRC.length)}:${text.slice(0, hit.index).split("\n").length}`);
  });
  expect(offenders).toEqual([]);
  for (const count of ["<Badge variant=\"outline\">{items.length}</Badge>", "<Badge>{count}</Badge>", "<Badge variant=\"cyan\">3</Badge>", "<Badge>\n  {totalBound}\n</Badge>"]) {
    expect(count.match(COUNT_ONLY_BADGE), count).not.toBeNull();
  }
  for (const chip of ["<Badge variant=\"outline\">{t_i18n(\"Paused\")}</Badge>", "<Badge>{span.type}</Badge>"]) {
    expect(chip.match(COUNT_ONLY_BADGE), chip).toBeNull();
  }
});

test("CountBadge stays round", () => {
  const code = readFileSync(join(SRC, "components/ui/count-badge.tsx"), "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
  expect(code).toMatch(/\brounded-full\b/);
  expect(code).toMatch(/\bmin-w-5\b/);
});

test("no new hand-made chip is drawn as a full pill", () => {
  expect(pillChipCounts()).toEqual(PENDING_PILL_CHIPS);
});

test("the pill pattern catches chips and leaves round shapes alone", () => {
  for (const chip of [
    "inline-flex items-center rounded-full border px-2.5 py-0.5 content-compact font-semibold",
    "rounded-full px-2 py-0.5 text-[11px] font-medium",
    "inline-flex items-center gap-1 rounded-full bg-elevation-hover px-2 py-0.5 text-xs",
  ]) {
    expect(isPillChip(chip), chip).toBe(true);
  }
  for (const round of [
    "inline-flex items-center rounded-sm border px-2 py-0.5 content-compact font-semibold",
    "h-8 w-8 animate-spin rounded-full border-2 border-accent border-t-transparent",
    "flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-bold",
    "h-1.5 w-full rounded-full bg-elevation-hover",
    "flex h-6 w-6 items-center justify-center rounded-full px-1 content-caption",
    "size-3.5 rounded-full border px-1 content-caption",
  ]) {
    expect(isPillChip(round), round).toBe(false);
  }
});
