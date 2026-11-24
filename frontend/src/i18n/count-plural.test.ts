/**
 * A count next to a counted noun goes through an ICU plural - `yarn test`.
 *
 * "continuing from its 1 completed tool calls" and "last 1 runs"
 * both reached UX review: a bare `{count}` before a plural noun reads
 * wrong as soon as the count is 1. The rule (CEO Office Developer skill,
 * section 13) is that such a count is written
 * `{count, plural, one {# run} other {# runs}}`.
 *
 * The guard reads `src/i18n/lang/en.json` with the ICU parser and fails on any
 * message where an argument outside a plural is followed by a plural noun of
 * a counted thing (up to two words between, "{count} specialist agents") or
 * by a hedged "noun(s)" ("{count} day(s) ago"), or directly preceded
 * by a plural noun ("runs {count}"). A message that is legitimately
 * bare goes in BARE with the reason. It also checks that fr and es carry a
 * plural wherever en does, and that ja keeps every count en shows through a
 * plural (ja has one plural form, so it may write the count bare or in a
 * single `other`).
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { parse, TYPE, type MessageFormatElement } from "@formatjs/icu-messageformat-parser";
import { createIntl } from "react-intl";

// vitest runs from frontend/ (CI and `yarn test`), as shadow-overlay-floating.test.ts.
const LANG = join(process.cwd(), "src", "i18n", "lang");
const catalog = (locale: string): Record<string, string> =>
  JSON.parse(readFileSync(join(LANG, `${locale}.json`), "utf8")) as Record<string, string>;

/** The counted things whose plural noun must not follow a bare count. */
const NOUNS = [
  "tool calls",
  "runs",
  "items",
  "messages",
  "members",
  "files",
  "agents",
  "errors",
  "days",
  "hours",
  "minutes",
  "seconds",
  "attempts",
  "retries",
  "pages",
  "slides",
  "results",
];
const NOUN = `(?:${NOUNS.join("|")})`;
// "{count} run(s)" hedges the plural instead of writing it, and reads "0 day(s) ago" or "1 terminée(s)".
const HEDGED = "[a-z]+\\((?:s|es)\\)";
const AFTER = new RegExp(`^\\s+(?:[a-z]+\\s+){0,2}?(?:${NOUN}\\b|${HEDGED})`);
const BEFORE = new RegExp(`\\b${NOUN} $`);

/** en keys whose count stays bare on purpose, each with why. */
const BARE: Record<string, string> = {
  "Proposals {name} files appear here for who may approve them.": "{name} is the Platform Health's name (\"files\" is a verb), not a count.",
  "A small pet in the corner of the screen shows what {name} and your runs are doing.": "{name} is the Platform Health's name, not a count.",
  "Connect external AI tools (Cursor, Claude Desktop, etc.) to your {platform} agents via MCP.":
    "{platform} is the product name, not a count.",
  "Consulting {count} specialist agents…": "Shown only when count > 1; one agent reads \"Consulting {name}…\".",
  "Page {page} lies more than {max} results from both ends of this list, so it is only reached by paging forward from the first page. Narrow the filters (a shorter date range, an agent, a status) to bring it within reach.":
    "{max} is the API offset bound, a constant in the thousands, formatted before it is passed.",
  "Page {page} lies more than {max} results from both ends of this sorted list, and a sorted list is only read that far from either end. Narrow the filters (a shorter date range, an agent, a status) to bring it within reach, or sort newest first to page through every result.":
    "{max} is the API offset bound, a constant in the thousands, formatted before it is passed.",
  "Showing the {count} most recent runs: filter by type to browse older ones.":
    "{count} is the merged window bound (100), a constant.",
  "Showing the {count} most recent of {total} files.": "Shown only when total exceeds the files listed, so total > 1.",
  "{n} work items": "The plural half of a one/many pair; n === 1 uses \"{n} work item\".",
  "This workspace still holds {n} work items: archive it instead, or clear its work items first.":
    "n === 1 uses the \"1 work item\" message.",
  "{name}: manual minutes {manual}, review minutes {review}":
    "{manual} and {review} are formatted values after a label (RoiHowComputed, a number or \"value (low to high)\"), not counts of a noun.",
  "{rate} of the runs reviewed (7 d)":
    "{rate} is a share formatted as a percent (FleetActivityCard, intl.formatNumber style percent), not a count.",
};

/**
 * en keys with a plural whose `one` branch drops `#` and has no `=0`, each
 * with why the count is never 0 where the message is shown. fr (and every
 * locale that puts 0 in `one`) would otherwise read 0 as "the one ..."
 * ("sur la dernière exécution" with no run).
 */
const NEVER_ZERO: Record<string, string> = {
  "{count, plural, one {Every hour} other {Every # hours}}":
    "The outside lens cadence_hours is 1 to 720 (AttackSurfacePass, OrganizationOutsideLensCard).",
  "{count, plural, one {Last # day} other {Last # days}}":
    "The CTEM context bar and widget settings pass CONTEXT_PERIODS (7, 30, 90) only (dashboard-context.ts).",
  "{count, plural, one {Its runs refuse this integration's tools until its owner gets access back or the binding is removed.} other {Its runs refuse the tools of these # integrations until its owner gets access back or the bindings are removed.}}":
    "OrphanedIntegrationsNotice returns null when orphaned_integrations is empty.",
  "{count, plural, one {The account that signed in did not grant this scope: calls that need it fail until the integration is re-authorized with it accepted.} other {The account that signed in did not grant these # scopes: calls that need them fail until the integration is re-authorized with all of them accepted.}}":
    "StatusCard renders it only when scopesNotGranted(integration).length > 0.",
  "); {count, plural, one {the other one needs} other {the other # need}} a manual re-embed.":
    "KBHealthWarnings shows it only in the branch where nonRetryableErrors !== 0.",
  "{count, plural, one {The CI of its pull request is running: when it completes, the platform hands the item to {agent} at once.} other {The CI of its # pull requests is running: when they complete, the platform hands the item to {agent} at once.}}":
    "waitingPullRequestCount returns pull_requests.length || 1, so an item waiting for CI waits on at least one pull request.",
  "{count, plural, one {It stays blocked until the CI of its pull request completes and the platform hands it over.} other {It stays blocked until the CI of its # pull requests completes and the platform hands it over.}}":
    "waitingPullRequestCount returns pull_requests.length || 1, so an item waiting for CI waits on at least one pull request.",
  "Only the first {count, plural, one {# item is} other {# items are}} drawn: narrow the filters to see the rest.":
    "WorkItemGraphView passes the constant GRAPH_NODE_CAP (500) and shows it only when the graph is truncated.",
  "Delete {count, plural, one {the only run} other {all # runs}} from this batch?":
    "EvaluationDetail builds each batch from its runs, so a batch holds at least one.",
  "from the last {count, plural, one {run} other {{shown} runs}}":
    "OverviewTab hides the subtitle when there is no run.",
  "{pending, plural, one {# in progress} other {# in progress}}, from the last {count, plural, one {run} other {{shown} runs}}":
    "OverviewTab shows it only with at least one run in progress, so the run count is at least 1.",
  "{count, plural, one {One word} other {Up to # words, separated by commas}}":
    "PollVote renders question.max_words, which the poll block schema bounds to 1..3 and poll.tsx defaults to 1.",
  "{count, plural, one {Its reply goes} other {Its # replies go}} with it. This cannot be undone.":
    "PresentationCommentsPanel shows it only when replyCount > 0.",
  "Stop asking for {n, plural, one {this tool} other {these # tools}} on every run carrying your identity, including scheduled ones nobody is watching?":
    "ApproveByToolDialog offers Approve always only once a tool is ticked; TaskHistory only with approvable runs selected.",
  "{count, plural, one {Delete a resource other people use?} other {Delete what # resources lend to other people?}}":
    "credential-sharing-notice falls back to its own title when items is empty.",
  "{count, plural, one {Share a resource with other people?} other {Share # resources with other people?}}":
    "credential-sharing-notice falls back to its own title when items is empty.",
  "{count, plural, one {Stop sharing a resource?} other {Stop sharing # resources?}}":
    "credential-sharing-notice falls back to its own title when items is empty.",
  "Setting a new embedding default re-embeds {count, plural, one {the knowledge base that holds} other {the # knowledge bases that hold}} indexed content.":
    "provider-deletion pushes this warning only when embedding.knowledge_base_count > 0, at both call sites.",
  "{count, plural, one {This agent references a variable that no longer exists. Its placeholder reaches the model as written until you replace or remove it.} other {This agent references # variables that no longer exist. Their placeholders reach the model as written until you replace or remove them.}}":
    "missing-variables-banner shows it only when deleted.length > 0.",
  "{count, plural, one {This agent uses a variable that has no value yet. It resolves to an empty value until you set one.} other {This agent uses # variables that have no value yet. They resolve to empty values until you set them.}}":
    "missing-variables-banner shows it only when unfilled.length > 0.",
  "{count, plural, one {One variable still needs a value: a secret is never copied with an agent, so set it before the agent can use it.} other {# variables still need a value: a secret is never copied with an agent, so set them before the agent can use them.}}":
    "The public agent page shows it only when needing.length > 0.",
  "Needs an action: {count, plural, one {the item it waits for was cancelled ({titles}), so it does not reopen by itself.} other {# items it waits for were cancelled ({titles}), so it does not reopen by itself.}}":
    "BlockedStatusBadge picks this tooltip only when cancelled.length > 0.",
  "On the {count, plural, one {# entry} other {# entries}} both ran, {after} scores {afterValue} against {beforeValue} for {before} ({change}, 95% interval {low} to {high}). {verdict}":
    "EvaluationCompare shows it only for a pair with a change, and pairedChange returns null without a paired entry, so count is at least 1.",
  "Queued: emailed once the {count, plural, one {announcement} other {# announcements}} recorded before it went out.":
    "model-announcements returns the queued status only when queued_behind > 0.",
  "{count, plural, one {# save} other {# saves}} since {date}, combined over the last {hours, plural, one {# hour} other {# hours}}. Edit what the email announces before sending it.":
    "The backend builds a draft only from at least one saved row (saves = len(rows)) and validates the window to 1-168 hours.",
  "{count, plural, one {A secret could not be read} other {# secrets could not be read}}":
    "vault-secrets renders the alert only when failing > 0.",
  "{count, plural, one {A secret could not be read from its vault} other {# secrets could not be read from their vault}}":
    "vault-secrets renders the alert only when failing > 0.",
  "{count, plural, one {Its completed tool call is kept: resuming continues from it instead of running it again.} other {Its # completed tool calls are kept: resuming continues from them instead of running them again.}}":
    "InterruptedTurnNotice picks it only when turn.tool_calls_kept > 0.",
  "Delete the {count, plural, one {# recording or AI shot} other {# recordings and AI shots}} matching your filters?":
    "VideoStudio offers select-all-matching only when total > items.length, so total > 0.",
  "Delete {count, plural, one {the selected recording or AI shot} other {the # selected recordings and AI shots}}?":
    "ListControlBar shows the bulk delete only with a selection, so picked.size > 0.",
  "{count, plural, one {One department and one rule from the # group tagged Org unit} other {One department and one rule from each of the # groups tagged Org unit}}: the group counts for the department of its name.":
    "DepartmentsSection shows the tip only when orgUnitGroups.length > 0.",
  "{total, plural, one {No failure in its last run} other {No failure in its last # runs}}":
    "healthReasons adds the clean reason only when judged > 0.",
};

/**
 * Plural arguments whose `one` branch drops the `#` another branch shows, with
 * no `=0` branch. A plural that only picks a grammatical form ("is" / "are")
 * shows `#` nowhere and reads right at 0 in every locale.
 */
function oneWithoutPound(ast: MessageFormatElement[], out = new Set<string>()): Set<string> {
  for (const el of ast) {
    if (el.type === TYPE.plural) {
      const one = el.options.one;
      // What a branch writes of the count: `#`, or an argument standing for it ("{shown} runs").
      const shown = (option: { value: MessageFormatElement[] }) =>
        new Set(option.value.flatMap((part) => (part.type === TYPE.pound ? ["#"] : part.type === TYPE.argument ? [part.value] : [])));
      if (one && !("=0" in el.options)) {
        const inOne = shown(one);
        const dropped = Object.entries(el.options)
          .filter(([name]) => name !== "one")
          .some(([, option]) => [...shown(option)].some((token) => !inOne.has(token)));
        if (dropped) out.add(el.value);
      }
    }
    if (el.type === TYPE.plural || el.type === TYPE.select) {
      for (const option of Object.values(el.options)) oneWithoutPound(option.value, out);
    }
    if (el.type === TYPE.tag) oneWithoutPound(el.children, out);
  }
  return out;
}

type Hit = { arg: string; text: string };

/** Bare counts followed or preceded by a counted noun, outside any plural or select. */
function bareCounts(ast: MessageFormatElement[]): Hit[] {
  const hits: Hit[] = [];
  ast.forEach((el, i) => {
    if (el.type !== TYPE.argument && el.type !== TYPE.number) return;
    const next = ast[i + 1];
    const prev = ast[i - 1];
    if (next?.type === TYPE.literal && AFTER.test(next.value)) hits.push({ arg: el.value, text: next.value.trim() });
    if (prev?.type === TYPE.literal && BEFORE.test(prev.value)) hits.push({ arg: el.value, text: prev.value.trim() });
  });
  return hits;
}

/** Names of the plural arguments anywhere in a message. */
function pluralArgs(ast: MessageFormatElement[], out = new Set<string>()): Set<string> {
  for (const el of ast) {
    if (el.type === TYPE.plural) out.add(el.value);
    if (el.type === TYPE.plural || el.type === TYPE.select) {
      for (const option of Object.values(el.options)) pluralArgs(option.value, out);
    }
    if (el.type === TYPE.tag) pluralArgs(el.children, out);
  }
  return out;
}

/** Names of the plural arguments whose count is shown (`#` in an option), not only used to pick a grammatical form. */
function shownPluralArgs(ast: MessageFormatElement[], out = new Set<string>()): Set<string> {
  for (const el of ast) {
    if (el.type === TYPE.plural && Object.values(el.options).some((option) => option.value.some((part) => part.type === TYPE.pound))) {
      out.add(el.value);
    }
    if (el.type === TYPE.plural || el.type === TYPE.select) {
      for (const option of Object.values(el.options)) shownPluralArgs(option.value, out);
    }
    if (el.type === TYPE.tag) shownPluralArgs(el.children, out);
  }
  return out;
}

/** Names of every argument anywhere in a message. */
function allArgs(ast: MessageFormatElement[], out = new Set<string>()): Set<string> {
  for (const el of ast) {
    if (el.type === TYPE.argument || el.type === TYPE.number || el.type === TYPE.date || el.type === TYPE.time) out.add(el.value);
    if (el.type === TYPE.plural || el.type === TYPE.select) {
      out.add(el.value);
      for (const option of Object.values(el.options)) allArgs(option.value, out);
    }
    if (el.type === TYPE.tag) allArgs(el.children, out);
  }
  return out;
}

const en = catalog("en");

test("every count before a counted noun in en.json is an ICU plural", () => {
  const offenders: string[] = [];
  for (const [key, message] of Object.entries(en)) {
    if (key in BARE) continue;
    for (const hit of bareCounts(parse(message))) offenders.push(`${JSON.stringify(key)}: {${hit.arg}} ${hit.text}`);
  }
  expect(offenders, "write the count as {name, plural, one {# thing} other {# things}}, or add the key to BARE with why").toEqual([]);
});

test("every BARE key still exists and still holds a bare count, so the list never goes stale", () => {
  for (const key of Object.keys(BARE)) {
    expect(en[key], key).toBeDefined();
    expect(bareCounts(parse(en[key])).length, key).toBeGreaterThan(0);
  }
});

test("a plural whose one branch drops # has an =0 branch in en and fr, or a NEVER_ZERO reason", () => {
  const fr = catalog("fr");
  const offenders: string[] = [];
  for (const [key, message] of Object.entries(en)) {
    if (key in NEVER_ZERO) continue;
    const args = new Set([...oneWithoutPound(parse(message)), ...(fr[key] ? oneWithoutPound(parse(fr[key])) : [])]);
    if (args.size > 0) offenders.push(`${JSON.stringify(key)}: {${[...args].join("}, {")}}`);
  }
  expect(offenders, "add an =0 branch in every locale, hide the message at 0, or add the key to NEVER_ZERO with why").toEqual([]);
});

test("every NEVER_ZERO key still exists and still needs the exemption, in en or fr", () => {
  const fr = catalog("fr");
  for (const key of Object.keys(NEVER_ZERO)) {
    expect(en[key], key).toBeDefined();
    const needed = oneWithoutPound(parse(en[key])).size + (fr[key] ? oneWithoutPound(parse(fr[key])).size : 0);
    expect(needed, key).toBeGreaterThan(0);
  }
});

test.each(["fr", "es"])("%s carries a plural wherever en does", (locale) => {
  const translated = catalog(locale);
  const missing: string[] = [];
  for (const [key, message] of Object.entries(en)) {
    const wanted = pluralArgs(parse(message));
    if (wanted.size === 0 || translated[key] === undefined) continue;
    const have = pluralArgs(parse(translated[key]));
    const lost = [...wanted].filter((name) => !have.has(name));
    if (lost.length > 0) missing.push(`${JSON.stringify(key)}: no plural on ${lost.join(", ")}`);
  }
  expect(missing).toEqual([]);
});

test("ja keeps every count en shows through a plural, bare or in a single other", () => {
  const ja = catalog("ja");
  const missing: string[] = [];
  for (const [key, message] of Object.entries(en)) {
    // A plural that only picks "becomes" or "become" has nothing to show in ja.
    const wanted = shownPluralArgs(parse(message));
    if (wanted.size === 0 || ja[key] === undefined) continue;
    const have = allArgs(parse(ja[key]));
    const lost = [...wanted].filter((name) => !have.has(name));
    if (lost.length > 0) missing.push(`${JSON.stringify(key)}: drops {${lost.join("}, {")}}`);
  }
  expect(missing).toEqual([]);
});

/** Every .ts / .tsx source file under src/, tests excluded. */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(path, out);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(path);
  }
  return out;
}

test("no plural argument is fed by formatCompactNumber, which hands ICU a string and renders NaN", () => {
  // "NaN docs": `#` formats the plural argument as a
  // number, and formatCompactNumber returns "1.5k". Pass the number to the
  // plural and the compact text as its own argument: `{ count: n, shown: formatCompactNumber(n) }`.
  const call = /t_i18n\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*\{([^{}]*)\}/g;
  const offenders: string[] = [];
  for (const file of sourceFiles(join(process.cwd(), "src"))) {
    const source = readFileSync(file, "utf8");
    for (const match of source.matchAll(call)) {
      if (!match[1].includes("plural")) continue;
      let names: Set<string>;
      try {
        names = pluralArgs(parse(match[1]));
      } catch {
        continue;
      }
      for (const name of names) {
        if (new RegExp(`(?:^|[\\s,])${name}\\s*:\\s*formatCompactNumber\\(`).test(match[2])) {
          offenders.push(`${file.slice(process.cwd().length + 1)}: ${JSON.stringify(match[1])} gets {${name}} from formatCompactNumber`);
        }
      }
    }
  }
  expect(offenders, "pass the number to the plural and the compact text as a separate argument ({shown})").toEqual([]);
});

test.each(["fr", "es"])("%s hedges no plural with a (s) / (es) ending", (locale) => {
  // "1 échouée(s)", "{count} listo(s)": the count is known, so the message says
  // it through a plural, or names the count as a figure ("Actifs : 3").
  // http(s) is a URL scheme, and "(s)" alone after a space is a unit of seconds.
  // A gender hedge ("géré(e)" over a generic {entity}) is not a plural and stays.
  const hedged = Object.entries(catalog(locale))
    .filter(([, message]) => /[a-zà-ÿ]\((?:s|es|x)\)/i.test(message.replace(/http\(s\)/g, "")))
    .map(([key]) => key)
    .filter((key) => key !== "Scope(s)");
  expect(hedged, "write the count as {count, plural, one {# chose} other {# choses}}").toEqual([]);
});

test.each([
  ["fr", "{count} registered", 1, "1 enregistré"],
  ["fr", "{count} registered", 3, "3 enregistrés"],
  ["es", "{count} ready", 1, "1 listo"],
  ["es", "{count} ready", 3, "3 listos"],
  ["fr", "{count} extra", 1, "1 supplémentaire"],
  ["es", "{count} extra", 3, "3 adicionales"],
])("%s %j reads right at %i", (locale, key, count, expected) => {
  const intl = createIntl({ locale, messages: catalog(locale) });
  expect(intl.formatMessage({ id: key }, { count })).toBe(expected);
});

// RoiHowComputed "Not counted" line (UX r3 B1 on #9961): fr and es agree the participle with each count.
const NOT_COUNTED =
  "Not counted: {rejected, plural, one {{rejectedShown} rejected} other {{rejectedShown} rejected}}; {unreviewed, plural, one {{unreviewedShown} unreviewed} other {{unreviewedShown} unreviewed}} counted apart";
test.each([
  ["en", 1, 3, "Not counted: 1 rejected; 3 unreviewed counted apart"],
  ["fr", 1, 1, "Non comptés\u00a0: 1 rejetée\u00a0; 1 non revue comptée à part"],
  ["fr", 2, 3, "Non comptés\u00a0: 2 rejetées\u00a0; 3 non revues comptées à part"],
  ["es", 1, 1, "No contadas: 1 rechazada; 1 sin revisar contada aparte"],
  ["es", 2, 3, "No contadas: 2 rechazadas; 3 sin revisar contadas aparte"],
])("%s Not counted line agrees at %i rejected, %i unreviewed", (locale, rejected, unreviewed, expected) => {
  const intl = createIntl({ locale, messages: catalog(locale) });
  // The compact text of each count is its own argument (#10770); the plural reads the number.
  const values = { rejected, rejectedShown: String(rejected), unreviewed, unreviewedShown: String(unreviewed) };
  expect(intl.formatMessage({ id: NOT_COUNTED }, values)).toBe(expected);
});

test("no en.json key hedges a plural with noun(s) next to a count", () => {
  const hedged = Object.keys(en).filter((key) => /\{[a-zA-Z_]+\}[^{}]*?\b[a-z]+\((?:s|es)\)/.test(key) || /^[a-z]+\((?:s|es)\)$/.test(key));
  expect(hedged, "write the count as {name, plural, one {# thing} other {# things}}").toEqual([]);
});
