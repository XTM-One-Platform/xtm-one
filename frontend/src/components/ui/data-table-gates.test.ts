/**
 * One records table - `yarn test` (slice 2 of #9378).
 *
 * Every list of records draws through `components/ui/data-table.tsx`
 * (`dev-docs/reference/data-tables.md`, "Gates"). These source scans fail the
 * build when a change adds:
 *   1. a raw `<table>` outside the data table and the content renderers;
 *   2. a second table component, or a new caller of `EntityTable`;
 *   3. a `<DataTable>` caller without search, a sortable column, a filter
 *      (or a `noFilters` reason) and pagination (or `virtual`);
 *   4. a hand-made pager (`<Pagination>`, `<ListControlBar>`) outside the table;
 *   6. a second pager on one list (a pager row above the rows as well as below:
 *      #10215 criterion 6, the count and pager sit once, in the footer);
 *   7. standard v2 (#10916): a records table off the shared toolbar (its own
 *      `<SearchInput>` beside the table, or `showToolbar={false}`), a table
 *      without the total, Columns or page size outside `EXEMPT`, a row height
 *      outside the two `ROW_HEIGHT` tokens (a caller's row or cell class setting a
 *      height, a padding or a wrap to a third line), a plain select as a filter;
 *   8. a column hidden by a window breakpoint class instead of `hideBelow`;
 *   9. a page-level "select the page" box or row above a card grid (the DataTable
 *      draws it first in its toolbar);
 *  10. a link in a cell (#12070, Sam 15 Nov 2026): a column cell drawing a `<Link>`,
 *      an `<a>` or an underline, or clamping past two lines; the whole row is the
 *      target (`onRowClick`, `rowOpens`, `rowHref`), every table looks the same.
 *
 * The PENDING lists hold the files not migrated yet. They only shrink: each
 * migration slice removes its files in the same change, and a file that no
 * longer trips its gate must leave the list (the "stale" tests), so the build
 * is green at every slice and every list is empty when the sweep closes.
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as enterprise-cta-guard.test.ts.
const SRC = join(process.cwd(), "src") + sep;
const TABLE = "components/ui/data-table.tsx";

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sources(full);
    } else if (entry.name.endsWith(".tsx") && !/\.test\.tsx$/.test(entry.name)) {
      yield full;
    }
  }
}

const FILES: { path: string; text: string }[] = [...sources(SRC)]
  .map((full) => ({ path: full.slice(SRC.length).split(sep).join("/"), text: readFileSync(full, "utf8") }))
  .sort((a, b) => a.path.localeCompare(b.path));

const matching = (pattern: RegExp) => FILES.filter(({ text }) => pattern.test(text)).map(({ path }) => path);

/** Tables that ARE the content (a document, a slide, a chart), not a list of records: allowed for good. */
const CONTENT = [
  "components/chat/ChatMarkdown.tsx",
  "components/custom-dashboard/charts/DataTable.tsx",
  // The part lengths of ONE snapshot element (original, placebo, deviation; at most four parts), not a list of records (#9378 slice 5).
  "components/evaluation/PlaceboSnapshotCard.tsx",
  // The bundle review's object matrix of ONE entry (expected bundle against its answers) and the field-by-field values of ONE object (#12282): the grid is the content.
  "components/evaluation/bundle-review/CoverageView.tsx",
  "components/evaluation/bundle-review/ReviewInspector.tsx",
  // ONE comparison's dimensions against its evaluations, and its runs' figures on three entry sets (#12282): a statistics grid, not a list of records.
  "components/evaluation/compare/comparability-table.tsx",
  "pages/EvaluationCompare.tsx",
  // ONE evaluation's configurations against its launches, each cell one run's figure (#2): a statistics grid; its ranked runs list is a DataTable.
  "components/evaluation/compare/leaderboard-tab.tsx",
  // A fleet's approval gates as a level matrix (#9769 S4, design round 2 A2): the grid is the content.
  "components/fleets/FleetAutonomyCatalog.tsx",
  // The same gate catalog read at ONE level in the setup step (#1373): each gated action and whether it waits, a fixed grid, not a list of records.
  "components/fleets/FleetGatesAtLevel.tsx",
  "components/presentation/SchemaFieldControls.tsx",
  "components/presentation/blocks/table.tsx",
  "components/report/blocks.tsx",
  // Field-by-field default vs current of ONE customization (#9836 R3b), not a list of records.
  "components/roi/RoiDefaultDiff.tsx",
  "pages/agent-detail/MemoryTab.tsx",
  // Inline per-case score grid inside one case; confirmed or migrated by slice 5.
  "pages/EvaluationRunDetail.tsx",
  // The fixed field mapping of ONE source and section in the import stepper (#11780): source field to profile field, read from a constant, not a list of records.
  "pages/settings/OrganizationImportDialog.tsx",
  // The fixed retention categories (#396, six rows read from the policy) with their figures: a settings grid, not a list of records (UX r2 on #405, s.3 n/a).
  "pages/settings/FileRetentionSection.tsx",
  // The fixed Platform Health schedules (#735, four specialists read from the settings) with their cron, next and last run: a settings grid, not a list of records.
  "pages/settings/HealthTriggerBlocks.tsx",
  "pages/xtm-one/ctem/CoverageMatrixCard.tsx",
  // The two readings of ONE CTEM score in a hover card (#11689): Measured x In your context, not a list of records.
  "pages/xtm-one/ctem/CtemScoreTip.tsx",
  // The posture formula (#11689): the five fixed stages x weight and both readings, the computation is the content.
  "pages/xtm-one/ctem/PostureExplainDialog.tsx",
  // CTEM v2 coverage heat map (#10277 V5): stages x modules matrix, the grid is the content.
  "pages/xtm-one/ctem/StatsCoverageCard.tsx",
  // CTEM stage heroes (#11549): the sr-only table is the text alternative of a chart, the chart's content.
  "pages/xtm-one/ctem/stage-heroes.tsx",
];

/** Gate 1: raw `<table>` records lists not migrated yet (slice in the comment). */
const PENDING_TABLES = [
  "components/fleets/FleetMembersView.tsx", // 4
  "pages/Datasets.tsx", // 5
  "pages/EvaluationDetail.tsx", // 5
  "pages/EvaluationRunCompare.tsx", // 5
];

/** Gate 2: the asset index list views still on the `EntityTable` preset (slices 4 and 7). */
const PENDING_ENTITY_TABLE = [
  "pages/Agents.tsx",
  "pages/CustomDashboards.tsx",
  "pages/CustomTools.tsx",
  "pages/Integrations.tsx",
  "pages/InvestigationPacks.tsx",
  "pages/KnowledgeBases.tsx",
  "pages/MCPServers.tsx",
  "pages/Prompts.tsx",
  "pages/Skills.tsx",
  "pages/Variables.tsx",
  "pages/flows/LibraryTab.tsx",
];

/** Gate 2: the only exported components named `...Table`. */
const TABLE_COMPONENTS = [
  "components/custom-dashboard/charts/DataTable.tsx", // content: a dashboard widget
  TABLE,
  "components/ui/entity-table.tsx", // removed with the last PENDING_ENTITY_TABLE file
];

type Rule = "search" | "sort" | "filters" | "pagination";

/** Gate 3: what each `<DataTable>` caller still lacks. */
const PENDING_CALLERS: Record<string, Rule[]> = {
  "components/ui/entity-table.tsx": ["search", "filters"], // the preset, removed by slices 4 and 7; pages since standard v2 (#10916)
};

/** Gate 4: hand-made pagers and toolbars not migrated yet (slice in the comment). */
const PENDING_PAGERS = [
  "components/fleets/FleetMembersView.tsx", // 4
  "pages/CustomDashboards.tsx", // 7
  "pages/DatasetDetail.tsx", // 5
  "pages/EvaluationDetail.tsx", // 5
  "pages/Files.tsx", // 4
  "pages/agent-detail/ChatsTab.tsx", // 4
  "pages/flows/FleetsTab.tsx", // 4
  "pages/flows/MapTab.tsx", // 4
  "pages/knowledge-base-detail/DocumentsTab.tsx", // 4
  "pages/prompt-detail/VersioningTab.tsx", // 4
  "pages/public/PublicAgents.tsx", // 4
  "pages/public/PublicCustomTools.tsx", // 4
  "pages/public/PublicMCPServers.tsx", // 4
  "pages/public/PublicPrompts.tsx", // 4
  "pages/public/PublicSkills.tsx", // 4
  "pages/settings/ApiKeysAdminSection.tsx", // 9
  "pages/skill-detail/VersioningTab.tsx", // 4
];

/**
 * Gate 7 exemptions (standard v2, dev-docs/reference/data-tables.md "Exemptions"): a table that
 * can never exceed one page draws no toolbar, no Columns and no page size. Each with its reason;
 * the UX Director accepts every entry. None by default.
 */
const EXEMPT: Record<string, string> = {
  "pages/integration-detail/openrouter/OpenRouterModelsTab.tsx":
    "the providers serving ONE model (a handful of rows); the page's own model table carries the toolbar",
};

/** Gate 7: asset index pages that still draw their own search above the EntityTable preset (slices 4 and 7). */
const PENDING_OWN_TOOLBAR: string[] = [];

/**
 * Gate 7: a `<SearchInput>` beside the table, the dialogs left out: a picker search inside a
 * `<DialogContent>` (an Add Binding dialog's agent picker) filters the dialog's own list, never
 * the table's rows (#9375 slice S-EVAL, pages/Intents.tsx).
 */
const ownSearch = (text: string) => /<SearchInput\b/.test(text.replace(/<DialogContent\b[\s\S]*?<\/DialogContent>/g, ""));

/** Gate 7: `<DataTable>` callers with no hideable column yet, so no Columns chooser (sweep slice in the comment). */
const PENDING_COLUMNS: string[] = [];

/** Gate 7: a plain select still used as a toolbar filter (becomes a filter pill in its slice). */
const PENDING_TOOLBAR_SELECTS: string[] = [];

/** Gate 10: tables whose cells still draw a link or an underline (#12070: none left since slice 3). */
const PENDING_CELL_LINKS: string[] = [];

/** Gate 10: what a cell must not draw (a link, an anchor, an underline) or do (clamp past line 2). */
const CELL_LINK = /<Link\b|<a\b|(?<![\w-])(?:hover:)?underline\b|line-clamp-[3-9]/;

/** Gate 10: the column array of each column set, up to its closing bracket (the cells only). */
const columnArrays = (text: string) =>
  columnSets(text).map((set) => {
    const end = /\n\s*\],?\s*\n|\n\s*\];/.exec(set);
    return end ? set.slice(0, end.index + end[0].length) : set;
  });

/** Gate 10: what a row action must not draw (a link or an anchor): row actions are buttons (UX r1 B1 of #1133). */
const ROW_ACTION_LINK = /<Link\b|<a\b/;

/**
 * Gate 10: the render of each `rowActions={...}` prop of a file: the inline function, or the body of
 * the local function it names (`rowActions={bindAction}` and `const bindAction = (row) => {...};`).
 */
function rowActionRenders(text: string): string[] {
  return propBlocks(text, "rowActions").map((block) => {
    const name = /^\s*(\w+)\s*\}?\s*$/.exec(block)?.[1] ?? /^\s*(?:\w+\s*\?\s*)?(\w+)\s*(?::\s*undefined)?\s*\}/.exec(block)?.[1];
    if (!name) return block;
    const start = new RegExp(`(?:const|function)\\s+${name}\\b`).exec(text);
    if (!start) return block;
    const rest = text.slice(start.index, start.index + 4000);
    const end = /\n {2,4}\};?\n/.exec(rest);
    return end ? rest.slice(0, end.index + end[0].length) : rest;
  });
}

/**
 * Gate 10: a cut text named whole only by a native `title` (one opening tag carrying a `truncate`
 * class and a `title=`): it opens no tooltip on keyboard focus. A cut text in a cell is a
 * `<TruncatedText>` (design standards s.3; UX r2 B2 of #1133).
 */
const TITLED_CUT = /<[A-Za-z][\w.]*\b[^<>]*\btruncate\b[^<>]*\btitle=|<[A-Za-z][\w.]*\b[^<>]*\btitle=[^<>]*\btruncate\b/;

/** Gate 10: tables whose cells still name a cut text with a native title (only shrinks; found when the gate landed). */
const PENDING_TITLED_CUTS: string[] = [
  "components/fleets/FleetGateReview.tsx",
  "components/fleets/FleetPendingApprovals.tsx",
  "components/health/HealthSignalsList.tsx",
  "components/health/ImprovementsInbox.tsx",
  "components/roi/RoiRecordsList.tsx",
  "components/roi/RoiReportParts.tsx",
  "pages/agent-detail/MemoryTab.tsx",
  "pages/agent-detail/ObservabilityTab.tsx",
  "pages/ai-adoption/GapsTab.tsx",
  "pages/analytics/BreakdownCard.tsx",
  "pages/analytics/ErrorsCard.tsx",
  "pages/profile/agent-accounts/AgentAccountDecisionsTab.tsx",
  "pages/profile/agent-accounts/AgentAccountMessagesSentTab.tsx",
  "pages/profile/agent-accounts/AgentAccountOverview.tsx",
  "pages/settings/FleetMemberPresetsSection.tsx",
  "pages/settings/TrackedOrganizations.tsx",
  "pages/settings/TrackedOrganizationsAiDraftReview.tsx",
  "pages/Traces.tsx",
  "pages/TraceSessions.tsx",
  "pages/Value.tsx",
  "pages/xtm-one/CtemCaseDetail.tsx",
];

/** Classes that give a row or a cell its own height, padding or a wrap past the second line. */
const ROW_SIZING = /\b(min-h-\S+|h-(\d+|\[)\S*|py-\S+|line-clamp-[3-9]|whitespace-pre-wrap|whitespace-normal)/;

/** The value of a JSX prop given as `name={...}`, up to the next prop of the same element. */
function propBlocks(text: string, name: string): string[] {
  return [...text.matchAll(new RegExp(`\\b${name}=\\{`, "g"))].map((match) => {
    const rest = text.slice((match.index ?? 0) + match[0].length, (match.index ?? 0) + match[0].length + 1500);
    const next = /\n\s+[a-zA-Z]+=[{"]/.exec(rest);
    return next ? rest.slice(0, next.index) : rest;
  });
}

/**
 * The column sets a file declares (`DataTableColumn<Row>[]`), each up to the next one: gate 7
 * asks a hideable column of every set, so every `<DataTable>` of the file draws Columns.
 */
function columnSets(text: string): string[] {
  const starts = [...text.matchAll(/DataTableColumn<[^>]*>\[\]/g)].map((match) => match.index ?? 0);
  // A bare type annotation (`useColumns(): DataTableColumn<Row>[] {`, `useMemo<...>(() => {`)
  // declares no column: only the sets that carry a `header:` are tables.
  return starts.map((start, index) => text.slice(start, starts[index + 1] ?? text.length)).filter((set) => /\bheader:/.test(set));
}

/**
 * Whether a column set has a hideable column past its first one: the table never hides its first
 * column (data-table.tsx), so a set whose only hideable column is the first draws no Columns
 * either (UX round 5 on #10980, B1).
 */
function hasHideableColumn(set: string): boolean {
  if (!/\bhideable\b/.test(set)) return false;
  // Only a set that writes its first column inline is read column by column; a set that starts
  // with a shared column (`[nameColumn, ...]`) keeps the plain test.
  if (!/^DataTableColumn<[^>]*>\[\]\s*=\s*\[\s*(\/\/[^\n]*\n\s*)*\{\s*key: /.test(set)) return true;
  const keys = [...set.matchAll(/\bkey: ["'`]/g)].map((match) => match.index ?? 0);
  return keys.length > 1 && /\bhideable\b/.test(set.slice(keys[1]));
}

/** A caller of the records table (the dashboard widget `charts/DataTable` is content, not this table). */
const USES_TABLE = /from "@\/components\/ui\/data-table"/;

/** The rules a `<DataTable>` caller fails, read on its source. */
function missingRules(text: string): Rule[] {
  const missing: Rule[] = [];
  if (!/\bsearchFields=|\bserver=/.test(text)) missing.push("search");
  if (!/\bsortValue\b/.test(text)) missing.push("sort");
  if (!/\bfilters=|\bnoFilters="[^"]+"/.test(text)) missing.push("filters");
  if (/\bpaginate=\{false\}/.test(text) && !/\bvirtual\b/.test(text)) missing.push("pagination");
  return missing;
}

describe("Data table gates (dev-docs/reference/data-tables.md)", () => {
  it("1. draws no raw <table> outside the data table and the content renderers", () => {
    const allowed = new Set([TABLE, ...CONTENT, ...PENDING_TABLES]);
    const offenders = matching(/<table\b/).filter((path) => !allowed.has(path));
    expect(offenders, "draw the records through components/ui/data-table.tsx (DataTable)").toEqual([]);
  });

  it("1. keeps PENDING_TABLES and CONTENT to the files that still draw a <table>", () => {
    const found = new Set(matching(/<table\b/));
    const stale = [...CONTENT, ...PENDING_TABLES].filter((path) => !found.has(path));
    expect(stale, "drop a migrated file from its list in the same change").toEqual([]);
  });

  it("2. adds no caller of EntityTable and no second table component", () => {
    const callers = matching(/from "@\/components\/ui\/entity-table"/);
    expect(callers.filter((path) => !PENDING_ENTITY_TABLE.includes(path)), "use DataTable, not the EntityTable preset").toEqual([]);
    const components = matching(/export (default )?(function|const) \w*Table\b/);
    expect(components.filter((path) => !TABLE_COMPONENTS.includes(path)), "one records table: extend DataTable").toEqual([]);
  });

  it("2. keeps PENDING_ENTITY_TABLE to the files that still import EntityTable", () => {
    const callers = new Set(matching(/from "@\/components\/ui\/entity-table"/));
    expect(PENDING_ENTITY_TABLE.filter((path) => !callers.has(path)), "drop a migrated file from PENDING_ENTITY_TABLE").toEqual([]);
  });

  it("3. gives every DataTable caller search, sort, a filter and pagination", () => {
    const offenders = FILES.filter(({ path, text }) => path !== TABLE && USES_TABLE.test(text) && /<DataTable\b/.test(text))
      .map(({ path, text }) => ({ path, missing: missingRules(text).filter((rule) => !(PENDING_CALLERS[path] ?? []).includes(rule)) }))
      .filter(({ missing }) => missing.length > 0);
    expect(offenders, "pass searchFields (or server), a sortValue, filters (or a noFilters reason), and keep the pager").toEqual([]);
  });

  it("3. keeps PENDING_CALLERS to the rules each caller still fails", () => {
    const stale = Object.entries(PENDING_CALLERS).flatMap(([path, rules]) => {
      const text = FILES.find((file) => file.path === path)?.text ?? "";
      const missing = missingRules(text);
      return rules.filter((rule) => !missing.includes(rule)).map((rule) => `${path}: ${rule}`);
    });
    expect(stale, "drop a rule the caller now meets from PENDING_CALLERS").toEqual([]);
  });

  it("4. draws no pager or list toolbar outside the data table", () => {
    const allowed = new Set([TABLE, ...CONTENT, ...PENDING_PAGERS]);
    const offenders = matching(/<(Pagination|ListControlBar)\b/).filter((path) => !allowed.has(path));
    expect(offenders, "page and filter the records through DataTable, not a hand-made list").toEqual([]);
  });

  it("4. keeps PENDING_PAGERS to the files that still draw one", () => {
    const found = new Set(matching(/<(Pagination|ListControlBar)\b/));
    expect(PENDING_PAGERS.filter((path) => !found.has(path)), "drop a migrated file from PENDING_PAGERS").toEqual([]);
  });

  it("5. gives every defaultPageSize a size the page size select offers", () => {
    // A size outside PAGE_SIZES renders an empty page size select (UX review of #9851, B1).
    const table = FILES.find(({ path }) => path === TABLE)?.text ?? "";
    const declared = /const PAGE_SIZES = \[([^\]]*)\]/.exec(table);
    expect(declared, "PAGE_SIZES is declared in the data table").not.toBeNull();
    const sizes = new Set((declared?.[1] ?? "").split(",").map((size) => Number(size.trim())));
    const offenders = FILES.flatMap(({ path, text }) =>
      [...text.matchAll(/defaultPageSize=\{([A-Za-z_][\w]*|\d+)\}/g)].map((match) => {
        const raw = match[1];
        const value = /^\d+$/.test(raw) ? raw : new RegExp(`const ${raw} = (\\d+);`).exec(text)?.[1];
        return sizes.has(Number(value)) ? null : `${path}: defaultPageSize ${raw} = ${value ?? "unresolved"}`;
      }),
    ).filter((offender): offender is string => offender !== null);
    expect(offenders, "use a size of PAGE_SIZES in data-table.tsx").toEqual([]);
  });

  it("6. draws one pager per list, in the footer", () => {
    // A file drawing two lists names them here, with the lists.
    const lists: Record<string, number> = {};
    // A `<ListControlBar>` draws a pager too, unless it hands it to the footer
    // (`paging="footer"`) or has none (`pager={false}`). Its props run to the
    // first self-closing tag (an arrow's `>` or a nested element may sit inside):
    // write these two props before any nested element.
    const barPagers = (text: string) =>
      (text.match(/<ListControlBar\b[\s\S]*?\/>/g) ?? []).filter((bar) => !/paging="footer"|pager=\{false\}/.test(bar)).length;
    const offenders = FILES.map(({ path, text }) => ({ path, pagers: (text.match(/<Pagination\b/g) ?? []).length + barPagers(text) }))
      .filter(({ path, pagers }) => pagers > (lists[path] ?? 1))
      .map(({ path, pagers }) => `${path}: ${pagers}`);
    expect(offenders, "one pager per list, under the rows (#10215 criterion 6)").toEqual([]);
    const table = FILES.find(({ path }) => path === TABLE)?.text ?? "";
    expect((table.match(/\{pagination\}/g) ?? []).length, "the data table draws its pager in the footer only").toBe(0);
  });

  it("6. hands the pager of a ListControlBar above a data table to the table's footer", () => {
    // The table draws the list's total and pager; a selection bar above it that
    // keeps its own range and pager draws a second count (UX review of #11095, B1).
    const offenders = FILES.filter(({ path, text }) => path !== TABLE && /<(DataTable|EntityTable)\b/.test(text))
      .flatMap(({ path, text }) =>
        (text.match(/<ListControlBar\b[\s\S]*?\/>/g) ?? []).filter((bar) => !/paging="footer"|pager=\{false\}/.test(bar)).map(() => path),
      );
    expect(offenders, 'pass paging="footer" to the ListControlBar above the table').toEqual([]);
  });

  it("6. says the total once, at the top: no bottom count under any list", () => {
    // Sam, 11 Nov 2026: a stray "N results" line under a table or a card grid while the
    // toolbar already shows the total. The shared pager draws nothing on a single page and
    // never a range or a total past one page (UX r1 B1 and B2 on #11192): every list says
    // its total at the top, with the shared TotalChip or its own header count.
    const pager = FILES.find(({ path }) => path.endsWith("components/ui/pagination.tsx"))?.text ?? "";
    expect(/if \(total === 0 \|\| singlePage\) return null;/.test(pager), "a single page draws no footer").toBe(true);
    expect(/# results?\}/.test(pager), "the pager draws no 'N results' line").toBe(false);
    expect(/Showing \{from\}/.test(pager), "the pager draws no range").toBe(false);
    expect(/totalShownAbove/.test(pager), "the pager has no switch that brings the range back").toBe(false);
    // Every page that draws the shared pager says the total above it: a TotalChip, a
    // ListControlBar with showTotal, or a counted header (CatalogPageHeader count, a
    // plural count line, the KB document count) read in the same file.
    const TOP_TOTAL = /<TotalChip\b|\bshowTotal\b|<CatalogPageHeader\b[^\n]*\bcount=\{|plural, one \{# (entry|member)\}|pluralize\(total, "document"\)/;
    const offenders = FILES.filter(
      ({ path, text }) => !/\.test\.tsx?$/.test(path) && !path.endsWith("components/ui/pagination.tsx") && /<Pagination\b/.test(text),
    )
      .filter(({ path, text }) => path !== TABLE && !TOP_TOTAL.test(text))
      .map(({ path }) => path);
    expect(offenders, "say the total at the top (TotalChip) of every list under a <Pagination>").toEqual([]);
  });

  it("6. draws every list total with the one shared TotalChip", () => {
    // One pattern, one component (UX r1 B3 on #11192): the data table, the ListControlBar
    // and every hand-made list toolbar draw the TotalChip, never a local chip markup.
    const table = FILES.find(({ path }) => path === TABLE)?.text ?? "";
    expect(/<TotalChip testId="data-table-total"/.test(table), "the data table's toolbar total is the TotalChip").toBe(true);
    const bar = FILES.find(({ path }) => path.endsWith("components/ui/list-control-bar.tsx"))?.text ?? "";
    expect(/<TotalChip testId="list-control-bar-total"/.test(bar), "the ListControlBar total is the TotalChip").toBe(true);
    const local = FILES.filter(({ path, text }) => !path.endsWith("components/ui/total-chip.tsx") && /aria-label=\{t_i18n\("Total"\)\}/.test(text) && /tabular-nums/.test(text))
      .map(({ path }) => path);
    expect(local, "use <TotalChip>, not a local total chip").toEqual([]);
  });

  const callers = FILES.filter(
    ({ path, text }) =>
      path !== TABLE &&
      ((USES_TABLE.test(text) && /<DataTable\b/.test(text)) || (/from "@\/components\/ui\/entity-table"/.test(text) && /<EntityTable\b/.test(text))),
  );

  it("7. draws the total, Columns and page size in the one toolbar, and two row heights only", () => {
    const table = FILES.find(({ path }) => path === TABLE)?.text ?? "";
    expect(/export const ROW_HEIGHT = \{ 1: "h-\d+", 2: "h-\d+" \} as const;/.test(table), "ROW_HEIGHT holds the two row heights only").toBe(true);
    expect((table.match(/\{totalChip\}/g) ?? []).length, "the total sits once, at the toolbar's right end").toBe(1);
    expect(/ROW_HEIGHT\[rowLines\]/.test(table), "every row takes its height from ROW_HEIGHT").toBe(true);
    // The row's cells carry no vertical padding, so the content never sets a height past
    // the token (UX round 1 on #10920, B1); the rendered heights are measured in
    // tests_e2e/agents-list-fit.spec.ts.
    expect(/const rowCellPadding = [^;]*py-0[^;]*;/.test(table) && !/const rowCellPadding = [^;]*py-[1-9]/.test(table), "row cells have no vertical padding").toBe(true);
    const entity = FILES.find(({ path }) => path.endsWith("components/ui/entity-table.tsx"))?.text ?? "";
    expect(/data-testid="entity-name-line2" className="[^"]*\bh-6\b/.test(entity), "line 2 of a two-line row has one fixed height, badge or not").toBe(true);
    // The scope switcher is never a filter: no caller counts it in externalFilterCount (B2).
    const scopeCounted = callers.filter(({ text }) => /externalFilterCount=\{[^}]*landingVisibilityFilter/.test(text)).map(({ path }) => path);
    expect(scopeCounted, "the scope is not a filter: Clear all never resets it").toEqual([]);
  });

  it("7. keeps every records table on the shared toolbar, with Columns and page size", () => {
    const offenders = callers.flatMap(({ path, text }) => {
      const found: string[] = [];
      if (ownSearch(text) && !PENDING_OWN_TOOLBAR.includes(path)) found.push(`${path}: its own search beside the table`);
      if (/showToolbar=\{false\}|paginate=\{false\}/.test(text) && !EXEMPT[path]) found.push(`${path}: no toolbar or no page size`);
      if (USES_TABLE.test(text) && !PENDING_COLUMNS.includes(path) && !EXEMPT[path]) {
        // Per column set, not per file: a second table of the same file without a hideable
        // column has no Columns chooser either (UX round 3 on #10980, B2).
        const bare = columnSets(text).filter((set) => !hasHideableColumn(set)).length;
        if (!/\bhideable\b/.test(text) || bare > 0) found.push(`${path}: no Columns (${bare || 1} table(s))`);
      }
      return found;
    });
    expect(offenders, "one toolbar: search, scope, filters, tags in the table; Columns and page size on every table (or an EXEMPT reason)").toEqual([]);
  });

  it("7. keeps PENDING_OWN_TOOLBAR, PENDING_COLUMNS and EXEMPT to the files that still need them", () => {
    const byPath = new Map(callers.map(({ path, text }) => [path, text]));
    const stale = [
      ...PENDING_OWN_TOOLBAR.filter((path) => !ownSearch(byPath.get(path) ?? "")),
      ...PENDING_COLUMNS.filter((path) => /\bhideable\b/.test(byPath.get(path) ?? "hideable")),
      ...Object.keys(EXEMPT).filter((path) => !byPath.has(path)),
    ];
    expect(stale, "drop a migrated file from its list in the same change").toEqual([]);
  });

  it("7. sets no row height, padding or third line from a caller", () => {
    const offenders = callers.flatMap(({ path, text }) =>
      [...propBlocks(text, "rowClassName"), ...[...text.matchAll(/\bclassName: ("[^"]*"|`[^`]*`)/g)].map((match) => match[1])]
        .filter((classes) => ROW_SIZING.test(classes))
        .map((classes) => `${path}: ${ROW_SIZING.exec(classes)?.[0]}`),
    );
    expect(offenders, "rows take one of the two ROW_HEIGHT heights (rowLines 1 or 2); a cell never wraps to a third line").toEqual([]);
  });

  it("7. uses no plain select as a toolbar filter", () => {
    // A slot given as a variable (`toolbar={addMember}`) is read where the variable is
    // declared: a picker hidden behind a name is still in the toolbar (UX r1 B2 on #11941).
    const resolved = (text: string, block: string) => {
      const name = /^\s*([A-Za-z_$][\w$]*)\s*\}?\s*$/.exec(block)?.[1];
      if (!name) return block;
      const at = text.search(new RegExp(`\\bconst ${name}\\b[^=]*=`));
      return at < 0 ? block : text.slice(at, at + 1500);
    };
    const selects = callers
      .filter(({ text }) =>
        [...propBlocks(text, "toolbar"), ...propBlocks(text, "filterPills")].some((block) => /<(Select|SearchableSelect|select)\b/.test(resolved(text, block))),
      )
      .map(({ path }) => path);
    expect(selects.filter((path) => !PENDING_TOOLBAR_SELECTS.includes(path)), "a filter is a pill (filters, filterPills with FilterPill or PropertyFilter), never a select").toEqual([]);
    expect(PENDING_TOOLBAR_SELECTS.filter((path) => !selects.includes(path)), "drop a migrated file from PENDING_TOOLBAR_SELECTS").toEqual([]);
  });

  it("7. marks no headerless column hideable: the Columns picker would list an empty row (#11289)", () => {
    // A column object whose header is blank and that asks to be hideable, in either order of
    // the two props. The table keeps such a column out of the picker anyway (isPickable); the
    // gate keeps a caller from asking for it, so a toggleable column always carries a label.
    const blankHideable = /\bheader:\s*(""|''|``)[^{}]*\bhideable:\s*true\b|\bhideable:\s*true\b[^{}]*\bheader:\s*(""|''|``)/;
    const found = FILES.filter(({ text }) => blankHideable.test(text)).map(({ path }) => path);
    expect(found, "give the column a header (an i18n key in every locale) or drop hideable").toEqual([]);
  });

  it("9. draws the page box of a card grid in the toolbar only, never a page-level select row", () => {
    // A selectable `renderGrid` list gets its page box from the DataTable, first in the toolbar
    // (Sam on /operations/shared-state, 14 Nov 2026: a lone checkbox row above the cards). A page that
    // draws its own "select the page" box, or a `*-page-select` row, brings that row back.
    const ownPageBox = /Select every row on this page|data-testid="[a-z-]*page-select"/;
    const found = FILES.filter(({ path, text }) => path !== TABLE && ownPageBox.test(text)).map(({ path }) => path);
    expect(found, "pass selection to the DataTable: it draws the grid's page box in its toolbar").toEqual([]);
  });

  it("10. draws no link, underline or third line in a cell: the whole row is the target (#12070)", () => {
    const offenders = callers
      .filter(({ path, text }) => USES_TABLE.test(text) && !PENDING_CELL_LINKS.includes(path))
      .flatMap(({ path, text }) => columnArrays(text).flatMap((set) => (CELL_LINK.test(set) ? [`${path}: ${CELL_LINK.exec(set)?.[0]}`] : [])));
    expect(offenders, "open the row with onRowClick (rowOpens) or rowHref; a cell draws plain text on at most two lines").toEqual([]);
  });

  it("10. keeps PENDING_CELL_LINKS to the files whose cells still draw a link", () => {
    const byPath = new Map(callers.map(({ path, text }) => [path, text]));
    const stale = PENDING_CELL_LINKS.filter((path) => !columnArrays(byPath.get(path) ?? "").some((set) => CELL_LINK.test(set)));
    expect(stale, "drop an aligned file from PENDING_CELL_LINKS in the same change").toEqual([]);
  });

  it("10. adds no tab stop of its own in a cell: a routed row is ONE tab stop (#12070, UX round 1 B1)", () => {
    // A cell trigger that must stay focusable outside a routed row reads useRowIsTarget()
    // (DateTimeCell, IconMark, BadgeFocusTrigger, InfoTip, CellTipText, TagList). A table whose
    // rows open nothing keeps its cell tab stops: the tooltip is reached from the keyboard there.
    const ownStop = /tabIndex=\{0\}/;
    const routed = /\b(?:onRowClick|rowHref)=\{/;
    const offenders = callers
      .filter(({ text }) => USES_TABLE.test(text) && routed.test(text))
      .flatMap(({ path, text }) => (columnArrays(text).some((set) => ownStop.test(set)) ? [path] : []));
    expect(offenders, "use a shared trigger that reads useRowIsTarget(), or tabIndex={rowIsTarget ? undefined : 0}").toEqual([]);
  });

  it("10. draws no cell button that does what the row does: a routed row is ONE tab stop (#12070, UX round 2 A1)", () => {
    // `onRowClick={(row) => open(row.id)}` and a cell `onClick={() => open(row.id)}`: the same
    // action twice, two tab stops. A table whose cell button IS the row's disclosure says so
    // with toggleInCell (the row is then not a stop).
    const offenders = callers
      .filter(({ text }) => USES_TABLE.test(text) && !/\btoggleInCell\b/.test(text))
      .flatMap(({ path, text }) => {
        const handler = /\bonRowClick=\{\(?\w*\)?\s*=>\s*(\w+)\(/.exec(text)?.[1];
        if (!handler) return [];
        const sameAction = new RegExp(`onClick=\\{\\(\\)\\s*=>\\s*${handler}\\(`);
        return columnArrays(text).some((set) => sameAction.test(set)) ? [`${path}: ${handler}`] : [];
      });
    expect(offenders, "draw the cell as plain text: the row opens it (onRowClick)").toEqual([]);
  });

  it("10. draws no link in a row action: row actions are buttons, the row is the link (#12070, UX r1 B1 of #1133)", () => {
    const offenders = callers
      .filter(({ text }) => USES_TABLE.test(text))
      .flatMap(({ path, text }) => rowActionRenders(text).flatMap((render) => (ROW_ACTION_LINK.test(render) ? [`${path}: ${ROW_ACTION_LINK.exec(render)?.[0]}`] : [])));
    expect(offenders, "draw a row action as a Button (navigate in its onClick); the row opens its target with rowHref or onRowClick").toEqual([]);
  });

  it("10. reads the render of a rowActions prop, inline or named", () => {
    const named = "const act = (row: R) => {\n    return <Link to=\"/x\">Go</Link>;\n  };\n<DataTable\n  rowActions={act}\n  rows={rows}\n/>";
    const inline = "<DataTable\n  rowActions={(row) => <a href=\"/x\">Go</a>}\n  rows={rows}\n/>";
    const clean = "<DataTable\n  rowActions={(row) => <Button onClick={go}>Go</Button>}\n  rows={rows}\n/>";
    expect(rowActionRenders(named).some((render) => ROW_ACTION_LINK.test(render))).toBe(true);
    expect(rowActionRenders(inline).some((render) => ROW_ACTION_LINK.test(render))).toBe(true);
    expect(rowActionRenders(clean).some((render) => ROW_ACTION_LINK.test(render))).toBe(false);
  });

  it("10. names no cut text with a native title in a cell: TruncatedText opens the tooltip on focus (UX r2 B2 of #1133)", () => {
    const offenders = callers
      .filter(({ path, text }) => USES_TABLE.test(text) && !PENDING_TITLED_CUTS.includes(path))
      .flatMap(({ path, text }) => (columnArrays(text).some((set) => TITLED_CUT.test(set)) ? [path] : []));
    expect(offenders, "draw the cut text with <TruncatedText text={...} />, with no title").toEqual([]);
  });

  it("10. keeps PENDING_TITLED_CUTS to the files whose cells still carry a titled cut text", () => {
    const byPath = new Map(callers.map(({ path, text }) => [path, text]));
    const stale = PENDING_TITLED_CUTS.filter((path) => !columnArrays(byPath.get(path) ?? "").some((set) => TITLED_CUT.test(set)));
    expect(stale, "drop an aligned file from PENDING_TITLED_CUTS in the same change").toEqual([]);
  });

  it("10. reads a titled cut text in either attribute order", () => {
    expect(TITLED_CUT.test('<span className="truncate" title={name}>')).toBe(true);
    expect(TITLED_CUT.test('<span title={name} className="block truncate">')).toBe(true);
    expect(TITLED_CUT.test("<TruncatedText text={name} />")).toBe(false);
    expect(TITLED_CUT.test('<span className="truncate">{name}</span>')).toBe(false);
  });

  it("8. hides no column with a window breakpoint: hideBelow keeps the Columns picker true", () => {
    // `hidden xl:table-cell` (or min-[1440px]:) on a column's className draws it display none
    // while the Columns picker shows it checked, and checking it brings nothing back (UX
    // Director on #11291, Agents.tsx). A column steps aside through `hideBelow`, which the
    // picker reads and overrides.
    const windowHidden = /\b(?:header)?[cC]lassName: ["`][^"`]*\bhidden (?:sm|md|lg|xl|2xl|min-\[\d+px\]):table-cell/;
    const found = callers.filter(({ text }) => windowHidden.test(text)).map(({ path }) => path);
    expect(found, "use the column's hideBelow (a container width) instead of a page breakpoint class").toEqual([]);
  });
});
