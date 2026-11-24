/**
 * No new direct lucide-react import in the product source - `yarn test`.
 *
 * The Filigran Design System is the single source of truth for every UI
 * decision (dev-docs/reference/frontend-design-system.md): a product glyph is
 * rendered with the library's `<Icon name>`, resolved through its
 * icon-mapping.json, never by importing a lucide-react component. A glyph the
 * library lacks is one entry in fds-migration/LIBRARY-FEEDBACK.md, never an
 * approximate glyph (program issue #10533, deliverable 5).
 *
 * 744 source files still import lucide-react directly. PENDING counts them per
 * folder (path from frontend/, two levels below src/) and can only shrink:
 *   - a file that adds a direct import, or a folder absent from PENDING, fails;
 *   - a codemod slice that migrates files lowers the folder's count in the same
 *     commit, and deletes the entry at 0 (a count left higher fails too).
 * Test files are out of scope: they render a fixture icon prop, not a product
 * glyph.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-class-name-assertions.test.ts.
const ROOT = process.cwd();
const SELF = "src/no-direct-lucide-import.test.ts";
const SOURCE_FILE = /\.(ts|tsx)$/;
const TEST_FILE = /\.test\.(ts|tsx)$/;

/** A static or dynamic import of the lucide-react package itself. */
const LUCIDE_IMPORT = /\bfrom\s+["']lucide-react["']|\bimport\(\s*["']lucide-react["']\s*\)/;

/** Folder (from frontend/, at most src/<a>/<b>) -> files importing lucide-react. Only ever shrinks. */
const PENDING: Record<string, number> = {
  src: 1,
  "src/components": 1,
  "src/components/api-keys": 2,
  "src/components/arcade": 4,
  "src/components/auth": 2,
  "src/components/browser-profiles": 1,
  "src/components/budget": 1,
  "src/components/chat": 1,
  // The chat panel's host: the glyphs the old chat page drew, moved here with
  // it (src/components/chat, src/pages and src/pages/chat dropped as many).
  // MessageParts keeps `Scan` and ChatWaitingGame `Trophy`: no design-system
  // glyph in 1.5.0 (audit table of #10911).
  "src/components/chat-panel": 1,
  "src/components/companion": 8,
  "src/components/health": 6,
  "src/components/custom-dashboard": 33,
  "src/components/desktop": 1,
  "src/components/evaluation": 15,
  "src/components/fleets": 25,
  "src/components/integrations": 5,
  "src/components/investigation": 9,
  "src/components/layout": 10,
  "src/components/memory": 1,
  "src/components/presentation": 24,
  "src/components/report": 2,
  "src/components/roi": 7,
  "src/components/runs": 1,
  "src/components/studio": 2,
  "src/components/traces": 10,
  "src/components/ui": 83,
  "src/components/video-studio": 19,
  "src/components/work-items": 3,
  "src/components/workspaces": 8,
  "src/dev": 1,
  "src/hooks": 1,
  "src/lib": 5,
  "src/lib/commands": 1,
  "src/pages": 110,
  "src/pages/agent-detail": 28,
  "src/pages/ai-adoption": 3,
  "src/pages/analytics": 8,
  "src/pages/assignment-detail": 14,
  "src/pages/chat": 15,
  // As src/components/chat-panel: `undo-2`, `phone`, `arrow-right-left`,
  // `file-down`, `folder-kanban`, `globe` and `user-plus` are not in the registry.
  "src/pages/chat-panel": 3,
  "src/pages/custom-tool-detail": 4,
  "src/pages/desktop": 1,
  "src/pages/flow-builder": 21,
  "src/pages/flows": 4,
  "src/pages/integration-detail": 26,
  "src/pages/knowledge-base-detail": 5,
  "src/pages/mcp-server-detail": 5,
  "src/pages/new-agent-wizard": 4,
  "src/pages/operations": 6,
  "src/pages/profile": 19,
  "src/pages/prompt-detail": 2,
  "src/pages/public": 7,
  "src/pages/relay-group-detail": 5,
  "src/pages/settings": 47,
  "src/pages/skill-detail": 5,
  "src/pages/template-detail": 3,
  "src/pages/xtm-one": 27,
};

function* sourceFiles(dir: string): Generator<string> {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules" && entry.name !== "dist") yield* sourceFiles(full);
    } else if (SOURCE_FILE.test(entry.name) && !TEST_FILE.test(entry.name)) {
      yield relative(ROOT, full).split(sep).join("/");
    }
  }
}

/** src/a/b/c/File.tsx -> src/a/b; src/a/File.tsx -> src/a; src/File.tsx -> src. */
const folderOf = (file: string): string => {
  const parts = file.split("/").slice(0, -1);
  return parts.slice(0, 3).join("/");
};

const countByFolder = (): Map<string, number> => {
  const counts = new Map<string, number>();
  for (const file of sourceFiles(join(ROOT, "src"))) {
    if (!LUCIDE_IMPORT.test(readFileSync(join(ROOT, file), "utf8"))) continue;
    const folder = folderOf(file);
    counts.set(folder, (counts.get(folder) ?? 0) + 1);
  }
  return counts;
};

test("the pattern catches static and dynamic lucide-react imports and nothing else", () => {
  for (const line of [
    'import { Bot } from "lucide-react";',
    "import { icons } from 'lucide-react';",
    'const { icons } = await import("lucide-react");',
    'export { X } from "lucide-react";',
  ]) {
    assert.match(line, LUCIDE_IMPORT, line);
  }
  for (const line of [
    'import { Icon } from "@filigran/design-system";',
    'import type { LucideIcon } from "@/lib/icon-types";',
    "// lucide-react glyphs move to the library Icon",
  ]) {
    assert.doesNotMatch(line, LUCIDE_IMPORT, line);
  }
});

test("folderOf keeps at most two levels below src", () => {
  assert.equal(folderOf("src/App.tsx"), "src");
  assert.equal(folderOf("src/pages/Agents.tsx"), "src/pages");
  assert.equal(folderOf("src/pages/settings/General.tsx"), "src/pages/settings");
  assert.equal(folderOf("src/components/ui/charts/bar.tsx"), "src/components/ui");
});

test("no folder imports lucide-react directly more than PENDING allows, and PENDING never goes stale", () => {
  const counts = countByFolder();
  const problems: string[] = [];
  for (const [folder, count] of counts) {
    const allowed = PENDING[folder] ?? 0;
    if (count > allowed) {
      problems.push(`${folder}: ${count} files import lucide-react, ${allowed} allowed: render <Icon name> from @filigran/design-system`);
    }
  }
  for (const [folder, allowed] of Object.entries(PENDING)) {
    const count = counts.get(folder) ?? 0;
    if (count < allowed) {
      problems.push(`${folder}: ${count} files left, lower PENDING to ${count}${count === 0 ? " (delete the entry)" : ""}`);
    }
  }
  assert.deepEqual(problems, [], `Direct lucide-react imports (${SELF}):\n${problems.join("\n")}`);
});

/**
 * A busy state renders the library <Spinner>, never a spinning loader glyph:
 * a bare animate-spin ignores prefers-reduced-motion, while Spinner animates
 * under motion-safe only (WCAG 2.3.3). Spinner is done in the library ROADMAP
 * and exported since 1.4.0 (UX round 1 on #10618, finding B1).
 */
const SPINNING_LOADER_GLYPH =
  /<Icon\b[^>]*name="loader[^"]*"[^>]*animate-spin|<Icon\b[^>]*animate-spin[^>]*name="loader|<(?:Loader2|LoaderCircle|Loader)\b[^>]*animate-spin|<Spinner\b[^>]*animate-spin/;

test("the spinning-loader pattern catches both attribute orders and nothing else", () => {
  assert.match('<Icon name="loader-circle" size={16} className="animate-spin" />', SPINNING_LOADER_GLYPH);
  assert.match('<Icon className="mr-1 animate-spin" name="loader" />', SPINNING_LOADER_GLYPH);
  assert.match('<Loader2 className="h-4 w-4 animate-spin" />', SPINNING_LOADER_GLYPH);
  assert.match('<LoaderCircle className="animate-spin size-3" aria-hidden />', SPINNING_LOADER_GLYPH);
  assert.match('<Spinner size="sm" className={cn("h-3", "animate-spin")} />', SPINNING_LOADER_GLYPH);
  assert.doesNotMatch('<Spinner size="sm" tone="inherit" />', SPINNING_LOADER_GLYPH);
  assert.doesNotMatch('<RefreshCw className="h-4 w-4 animate-spin" />', SPINNING_LOADER_GLYPH);
  assert.doesNotMatch('<Icon name="loader-circle" size={16} />', SPINNING_LOADER_GLYPH);
});

test("no source renders a spinning loader glyph instead of the library Spinner", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(join(ROOT, "src"))) {
    if (SPINNING_LOADER_GLYPH.test(readFileSync(join(ROOT, file), "utf8"))) offenders.push(file);
  }
  assert.deepEqual(offenders, [], `Render <Spinner size="sm" /> from @filigran/design-system (${SELF}):\n${offenders.join("\n")}`);
});

/**
 * Spinner RULE-01 (design system 1.4.0): never the only signal. A ternary
 * that swaps a control's text for an unlabelled Spinner leaves it with no
 * accessible name while busy: keep the text beside it, or pass label=
 * (UX round 2 on #10618, finding B1).
 */
const LABEL_SWAPPED_FOR_SPINNER = /\?\s*\(?\s*<Spinner\b(?![^>]*\blabel=)[^>]*\/>\s*\)?\s*:|:\s*\(?\s*<Spinner\b(?![^>]*\blabel=)[^>]*\/>\s*\)?\s*\}/;

test("the label-swap pattern catches an unlabelled Spinner in either ternary branch and nothing else", () => {
  assert.match('{busy ? (\n  <Spinner size="sm" tone="inherit" />\n) : (\n  t("Save")\n)}', LABEL_SWAPPED_FOR_SPINNER);
  assert.match('{idle ? t("Save") : <Spinner size="sm" />}', LABEL_SWAPPED_FOR_SPINNER);
  assert.doesNotMatch('{busy ? <Spinner size="sm" label={t("Saving")} /> : t("Save")}', LABEL_SWAPPED_FOR_SPINNER);
  assert.doesNotMatch('{busy && <Spinner size="sm" tone="inherit" />}\n{t("Save")}', LABEL_SWAPPED_FOR_SPINNER);
});

test("no control swaps its text for an unlabelled Spinner", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(join(ROOT, "src"))) {
    if (LABEL_SWAPPED_FOR_SPINNER.test(readFileSync(join(ROOT, file), "utf8"))) offenders.push(file);
  }
  assert.deepEqual(offenders, [], `Keep the text beside the Spinner or pass label= (${SELF}):\n${offenders.join("\n")}`);
});

/**
 * Spinner RULE-02 (design system 1.4.0): the busy state belongs to the
 * control. A Button that shows `{busy && <Spinner />}` beside its text sets
 * aria-busy itself, so assistive technology hears the state the glyph shows
 * (UX round 1 on #10936, finding B1). The spinner sweep of #10533 migrated
 * every folder, so the guard covers the whole of src/.
 */
const BUSY_BUTTON = /<Button\b(?:=>|[^>])*>\s*\{[^{}]*&&\s*<Spinner\b/g;
const ARIA_BUSY_FOLDERS = ["src"];

function busyButtonsWithoutAriaBusy(source: string): string[] {
  return (source.match(BUSY_BUTTON) ?? []).filter((tag) => !/\baria-busy=/.test(tag.slice(0, tag.lastIndexOf(">"))));
}

test("the busy-button pattern needs aria-busy on the Button that shows the Spinner, and nothing else", () => {
  assert.equal(busyButtonsWithoutAriaBusy('<Button onClick={() => void save()} disabled={saving}>\n  {saving && <Spinner size="sm" tone="inherit" />}\n  {t("Save")}\n</Button>').length, 1);
  assert.equal(busyButtonsWithoutAriaBusy('<Button onClick={() => void save()} aria-busy={saving || undefined}>\n  {saving && <Spinner size="sm" tone="inherit" />}\n  {t("Save")}\n</Button>').length, 0);
  assert.equal(busyButtonsWithoutAriaBusy('<Button onClick={close}>{t("Cancel")}</Button>\n<div role="status">{loading && <Spinner size="sm" />}</div>').length, 0);
});

test("every busy Button of a migrated folder sets aria-busy", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(join(ROOT, "src"))) {
    if (!ARIA_BUSY_FOLDERS.some((folder) => file.startsWith(`${folder}/`))) continue;
    const missing = busyButtonsWithoutAriaBusy(readFileSync(join(ROOT, file), "utf8"));
    if (missing.length) offenders.push(`${file}: ${missing.length}`);
  }
  assert.deepEqual(offenders, [], `Set aria-busy={busy || undefined} on the Button (${SELF}):\n${offenders.join("\n")}`);
});

/**
 * Layout: inside a Button the gap spaces the Spinner from the text, so a
 * margin on the Spinner doubles it (UX round 1 on #11018, finding B2).
 */
const SPINNER_MARGIN_IN_BUTTON = /<Button\b(?:=>|[^>])*>(?:(?!<\/Button>)[\s\S])*?<Spinner\b[^>]*className="[^"]*\b(?:mr|ml|me|ms|mx)-/g;

function spinnerMarginsInButtons(source: string): string[] {
  return source.match(SPINNER_MARGIN_IN_BUTTON) ?? [];
}

test("the spinner-margin pattern catches a margin on a Spinner inside a Button, and nothing else", () => {
  assert.equal(spinnerMarginsInButtons('<Button disabled={busy}>\n  {busy && <Spinner size="sm" tone="inherit" className="mr-1" />}\n  {t("Save")}\n</Button>').length, 1);
  assert.equal(spinnerMarginsInButtons('<Button disabled={busy}>\n  {busy && <Spinner size="sm" tone="inherit" />}\n  {t("Save")}\n</Button>').length, 0);
  assert.equal(spinnerMarginsInButtons('<Button>{t("Close")}</Button>\n<div><Spinner size="md" className="mr-2" /> {t("Loading")}</div>').length, 0);
});

test("no Spinner inside a Button carries a horizontal margin", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(join(ROOT, "src"))) {
    const found = spinnerMarginsInButtons(readFileSync(join(ROOT, file), "utf8"));
    if (found.length) offenders.push(`${file}: ${found.length}`);
  }
  assert.deepEqual(offenders, [], `Drop the margin, the Button gap spaces the Spinner (${SELF}):\n${offenders.join("\n")}`);
});
