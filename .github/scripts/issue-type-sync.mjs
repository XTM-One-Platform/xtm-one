// Keeps an issue's GitHub Type and its primary type label in line with
// .github/LABELS.md. Run by .github/workflows/issue-type-sync.yml for
// one issue on an `issues` event, or on a sweep for every issue - open or
// closed - updated in the last SWEEP_DAYS days (0 = every issue).
// Dependency-free on purpose: Node's own fetch, nothing to install on the
// runner.
import { appendFileSync, readFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import { pathToFileURL } from "node:url";

export const KINDS = {
  feature: { label: "feature", type: "Feature" },
  bug: { label: "bug", type: "Bug" },
  documentation: { label: "documentation", type: "Task" },
  task: { label: null, type: "Task" },
};
export const TYPE_LABELS = ["feature", "bug", "documentation"];
export const DEPRECATED_LABELS = ["enhancement", "feature-request", "feature request", "improvement", "nice-to-have"];
const AIKIDO_LABEL = "Aikido";
// Every label whose addition or removal can change what the rules decide.
// The workflow starts a run for these labels and no other; a test holds the
// two lists together.
export const WATCHED_LABELS = [...TYPE_LABELS, ...DEPRECATED_LABELS, AIKIDO_LABEL];

// A Type the organization adds later (an Epic, an Incident) is not ours to
// overwrite: an issue carrying one is left alone.
const MANAGED_TYPES = ["Task", "Bug", "Feature"];

// `bug:` is not a Conventional Commits type, but hundreds of issues predate
// the convention with it and it means what `fix:` means.
export const PREFIX_KINDS = {
  feat: "feature",
  fix: "bug",
  bug: "bug",
  docs: "documentation",
  chore: "task",
  test: "task",
  refactor: "task",
  ci: "task",
  perf: "task",
  style: "task",
  build: "task",
  revert: "task",
};

export function titleKind(title) {
  const match = /^\s*([A-Za-z]+)(?:\([^)]*\))?!?\s*:/.exec(title ?? "");
  return match ? (PREFIX_KINDS[match[1].toLowerCase()] ?? null) : null;
}

function kindOfType(type, labels) {
  if (type === "Bug") return "bug";
  if (type === "Feature") return "feature";
  if (type === "Task") return labels.includes("documentation") ? "documentation" : "task";
  return null;
}

function kindOfLabels(labels, type) {
  const present = TYPE_LABELS.filter((label) => labels.includes(label));
  if (present.length === 1) return present[0];
  return present.find((label) => kindOfType(type, [label]) === label) ?? null;
}

function decideKind(issue, event) {
  const { title, labels, type } = issue;
  const fromTitle = titleKind(title);
  if (fromTitle) return [fromTitle, "title prefix"];
  // Without a prefix, the change that triggered this run is the freshest
  // intent - but only while it still holds, the issue is re-read first.
  if (event.action === "labeled" && labels.includes(event.label)) {
    if (TYPE_LABELS.includes(event.label)) return [event.label, `label "${event.label}" added`];
    if (DEPRECATED_LABELS.includes(event.label)) return ["feature", `label "${event.label}" added`];
  }
  if (event.action === "typed" && kindOfType(type, labels)) return [kindOfType(type, labels), `Type set to ${type}`];
  const fromLabels = kindOfLabels(labels, type);
  if (fromLabels) return [fromLabels, `label "${fromLabels}"`];
  const fromType = kindOfType(type, labels);
  if (fromType) return [fromType, `Type ${type}`];
  if (labels.some((label) => DEPRECATED_LABELS.includes(label))) return ["feature", "deprecated feature label"];
  if (labels.includes(AIKIDO_LABEL)) return ["bug", "Aikido finding"];
  return [null, "no signal, left for triage"];
}

// issue: { title, labels: string[], type: string | null }
// event: the change that triggered the run, if any: { action, label }
// Returns what to change; `type` is null when the current Type is right.
export function plan(issue, event = {}) {
  const none = { kind: null, reason: "", add: [], remove: [], type: null };
  if (issue.type && !MANAGED_TYPES.includes(issue.type)) return { ...none, reason: `custom Type ${issue.type}` };
  const [kind, reason] = decideKind(issue, event);
  const add = [];
  const remove = [];
  let type = null;
  if (kind) {
    const target = KINDS[kind];
    if (target.label && !issue.labels.includes(target.label)) add.push(target.label);
    for (const label of issue.labels) {
      if ((TYPE_LABELS.includes(label) && label !== target.label) || DEPRECATED_LABELS.includes(label)) remove.push(label);
    }
    if (issue.type !== target.type) type = target.type;
  }
  return { kind, reason, add, remove, type };
}

export function hasChanges(change) {
  return change.add.length > 0 || change.remove.length > 0 || change.type !== null;
}

// A blank value is the default; anything but a whole number of days is a
// mistake to refuse, never a reason to sweep every issue.
export function parseSweepDays(value) {
  const text = String(value ?? "").trim();
  if (text === "") return 3;
  if (!/^\d+$/.test(text)) throw new Error(`days must be a whole number of days (0 = every issue), got "${value}"`);
  return Number(text);
}

export const DEFAULTS = {
  attempts: 3,
  // The longest single wait worth sitting through; a longer one ends the run.
  maxWaitMs: 5 * 60_000,
  // How long a sweep may wait in total before it stops and reports.
  totalWaitMs: 15 * 60_000,
  // A sweep stops by itself well before the job times out, so it always
  // reports what it did and what it left.
  timeLimitMs: 40 * 60_000,
  // GITHUB_TOKEN's hourly budget (1,000 requests on standard plans) is shared
  // by every workflow of the repository: a sweep leaves this share of it.
  reserveShare: 0.2,
  minReserve: 100,
  // One write a second on average, under GitHub's secondary limit of 80
  // content-creating requests a minute.
  paceMs: 1_000,
  // An issue changed this recently may still have its own event run queued.
  settleMs: 10 * 60_000,
  now: Date.now,
  sleep,
};

export class HttpError extends Error {
  constructor(method, path, status, text) {
    super(`${method} ${path}: HTTP ${status} ${text}`);
    this.status = status;
  }
}

export class RateLimitError extends Error {
  constructor(method, path, waitMs) {
    super(`${method} ${path}: rate limited, GitHub asks to wait ${Math.ceil(waitMs / 1000)} s`);
    this.waitMs = waitMs;
  }
}

// How long GitHub asks to wait, or null when the response is no rate limit (a
// plain 403 is a permission error). Per "Rate limits for the REST API":
// `retry-after` first, then `x-ratelimit-reset` once nothing remains,
// otherwise at least a minute.
export function rateLimitWait(status, headers, text, now) {
  if (status !== 403 && status !== 429) return null;
  const retryAfter = headers.get("retry-after");
  if (retryAfter !== null && retryAfter.trim() !== "") {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds)) return Math.max(0, seconds) * 1000;
    const at = Date.parse(retryAfter);
    if (Number.isFinite(at)) return Math.max(0, at - now);
  }
  const reset = Number(headers.get("x-ratelimit-reset"));
  if (headers.get("x-ratelimit-remaining") === "0" && reset > 0) return Math.max(0, reset * 1000 - now) + 1000;
  if (status === 429 || /rate limit/i.test(text)) return 60_000;
  return null;
}

export function createClient({ token, fetch = globalThis.fetch, now = Date.now }) {
  // The primary rate limit as GitHub last reported it.
  const budget = { limit: null, remaining: null };
  async function request(method, path, body) {
    const response = await fetch(`https://api.github.com${path}`, {
      method,
      // A transferred issue answers 301: following it would read and write an
      // issue of another repository.
      redirect: "manual",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const limit = Number(response.headers.get("x-ratelimit-limit"));
    if (limit > 0) Object.assign(budget, { limit, remaining: Number(response.headers.get("x-ratelimit-remaining")) });
    if (response.ok) return response.status === 204 ? null : response.json();
    const text = await response.text();
    const waitMs = rateLimitWait(response.status, response.headers, text, now());
    if (waitMs !== null) throw new RateLimitError(method, path, waitMs);
    throw new HttpError(method, path, response.status, text);
  }
  return { request, budget };
}

// Runs `task` again, from its first request, after a rate limit with a short
// deadline or a transient 5xx: a retry never finishes half-done writes with a
// decision taken before the wait. Repeated failures back off exponentially.
async function withRetry(task, options, waited) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await task();
    } catch (error) {
      const base = error instanceof RateLimitError ? error.waitMs : error instanceof HttpError && error.status >= 500 ? 5_000 : null;
      if (base === null || attempt >= options.attempts) throw error;
      const waitMs = Math.max(base, 1_000) * 2 ** (attempt - 1);
      if (waitMs > options.maxWaitMs || waited.ms + waitMs > options.totalWaitMs) throw error;
      console.log(`${error.message} - retrying in ${Math.ceil(waitMs / 1000)} s`);
      waited.ms += waitMs;
      await options.sleep(waitMs);
    }
  }
}

function toIssue(raw) {
  return {
    number: raw.number,
    title: raw.title,
    state: raw.state,
    updatedAt: raw.updated_at,
    labels: raw.labels.map((label) => (typeof label === "string" ? label : label.name)),
    type: raw.type?.name ?? null,
  };
}

// null when the issue is gone: deleted (410), missing (404) or transferred to
// another repository (301).
async function readIssue(client, repo, number) {
  try {
    return toIssue(await client.request("GET", `/repos/${repo}/issues/${number}`));
  } catch (error) {
    if (error instanceof HttpError && [301, 404, 410].includes(error.status)) return null;
    throw error;
  }
}

function emptyApplied() {
  return { writes: 0, add: [], remove: [], type: null };
}

// Applies a plan, recording in `applied` every write GitHub accepted.
async function apply(client, repo, number, change, applied) {
  if (change.add.length) {
    await client.request("POST", `/repos/${repo}/issues/${number}/labels`, { labels: change.add });
    applied.writes++;
    applied.add.push(...change.add);
  }
  for (const label of change.remove) {
    try {
      await client.request("DELETE", `/repos/${repo}/issues/${number}/labels/${encodeURIComponent(label)}`);
      applied.writes++;
      applied.remove.push(label);
    } catch (error) {
      // Removed by someone else meanwhile.
      if (!(error instanceof HttpError && error.status === 404)) throw error;
    }
  }
  if (change.type) {
    // Without push access GitHub answers 200 and silently keeps the old Type.
    const updated = await client.request("PATCH", `/repos/${repo}/issues/${number}`, { type: change.type });
    applied.writes++;
    if (updated.type?.name !== change.type) {
      throw new Error(`GitHub dropped the Type change to ${change.type} - the token needs push access`);
    }
    applied.type = change.type;
  }
}

function emptyReport() {
  return { changes: [], failures: [], untriaged: [], deferred: [], left: [], stopped: null };
}

function recordChange(report, number, reason, applied) {
  if (applied.writes === 0) return;
  report.changes.push({ number, reason, add: applied.add, remove: applied.remove, type: applied.type });
}

// One issue, on its `issues` event. Runs of one issue never overlap (the
// workflow's concurrency group), so the issue is read, decided and written
// in one go.
export async function syncEvent(client, repo, payload, options = {}) {
  const settings = { ...DEFAULTS, ...options };
  const number = payload.issue.number;
  const event = { action: payload.action, label: payload.label?.name };
  const report = emptyReport();
  const applied = emptyApplied();
  let reason = "";
  try {
    await withRetry(
      async () => {
        const issue = await readIssue(client, repo, number);
        if (!issue) return;
        const change = plan(issue, event);
        reason = change.reason;
        if (hasChanges(change)) await apply(client, repo, number, change, applied);
      },
      settings,
      { ms: 0 },
    );
  } catch (error) {
    if (!(error instanceof RateLimitError)) throw new Error(`#${number}: ${error.message}`, { cause: error });
    // The issue was just updated, so the next sweep's window holds it.
    report.stopped = `${error.message}; the next sweep syncs #${number}`;
    report.left.push(number);
  }
  recordChange(report, number, reason, applied);
  return report;
}

// Every issue updated in the last `days` days (0 = every issue), open or
// closed. A sweep cannot share the per-issue concurrency groups of the event
// runs, so it guards each issue instead: it leaves alone an issue changed
// shortly before it started or while it runs - that change has its own event
// run, which decides - re-reads an issue right before writing it, and writes
// it in one burst, pausing between issues only.
export async function sweep(client, repo, options = {}) {
  const settings = { ...DEFAULTS, days: 3, ...options };
  const startedAt = settings.now();
  const report = emptyReport();
  const waited = { ms: 0 };
  const budgetLeft = () => {
    const { limit, remaining } = client.budget;
    if (limit === null || remaining >= Math.max(settings.minReserve, Math.ceil(limit * settings.reserveShare))) return null;
    return `the API budget is down to ${remaining} of ${limit} requests this hour, kept for the other workflows`;
  };

  // Every candidate is listed before anything is written, and sorted by
  // creation, so neither the sweep's own edits nor anyone else's shift the
  // pages being read.
  const candidates = [];
  const since = settings.days > 0 ? new Date(startedAt - settings.days * 86_400_000).toISOString() : null;
  try {
    for (let page = 1; ; page++) {
      const low = budgetLeft();
      if (low) {
        report.stopped = `${low}; the issues past page ${page - 1} of the list were not read`;
        break;
      }
      const query = new URLSearchParams({ state: "all", sort: "created", direction: "desc", per_page: "100", page: String(page) });
      if (since) query.set("since", since);
      const batch = await withRetry(() => client.request("GET", `/repos/${repo}/issues?${query}`), settings, waited);
      for (const raw of batch) {
        // The issues list returns pull requests too.
        if (raw.pull_request) continue;
        const issue = toIssue(raw);
        const change = plan(issue);
        if (!change.kind && issue.state === "open" && !issue.type) report.untriaged.push(issue.number);
        if (hasChanges(change)) candidates.push(issue);
      }
      if (batch.length < 100) break;
    }
  } catch (error) {
    if (!(error instanceof RateLimitError)) throw error;
    report.stopped = `${error.message}; the rest of the list was not read`;
  }

  for (const [index, candidate] of candidates.entries()) {
    const overtime = settings.now() - startedAt > settings.timeLimitMs;
    report.stopped ??= budgetLeft() ?? (overtime ? `the sweep reached its ${settings.timeLimitMs / 60_000}-minute time limit` : null);
    if (report.stopped) {
      report.left.push(...candidates.slice(index).map((issue) => issue.number));
      break;
    }
    if (Date.parse(candidate.updatedAt) > startedAt - settings.settleMs) {
      report.deferred.push(candidate.number);
      continue;
    }
    const applied = emptyApplied();
    let reason = "";
    try {
      const deferred = await withRetry(
        async () => {
          const issue = await readIssue(client, repo, candidate.number);
          if (!issue) return false;
          // Changed since it was listed, and not by this sweep's own writes.
          if (applied.writes === 0 && issue.updatedAt !== candidate.updatedAt) return true;
          const change = plan(issue);
          reason = change.reason;
          if (hasChanges(change)) await apply(client, repo, issue.number, change, applied);
          return false;
        },
        settings,
        waited,
      );
      if (deferred) report.deferred.push(candidate.number);
    } catch (error) {
      if (error instanceof RateLimitError) {
        report.stopped = error.message;
        report.left.push(...candidates.slice(index).map((issue) => issue.number));
      } else {
        report.failures.push(`#${candidate.number}: ${error.message}`);
      }
    }
    recordChange(report, candidate.number, reason, applied);
    if (report.stopped) break;
    if (applied.writes) await settings.sleep(settings.paceMs * applied.writes);
  }
  return report;
}

function describe(change) {
  return [
    change.add.length ? `+${change.add.join(", +")}` : "",
    change.remove.length ? `-${change.remove.join(", -")}` : "",
    change.type ? `Type -> ${change.type}` : "",
  ]
    .filter(Boolean)
    .join(" ");
}

const issueList = (numbers) => numbers.map((number) => `#${number}`).join(", ");

export function summarize(report) {
  const summary = [`### Issue type sync: ${report.changes.length} issue(s) updated`, ""];
  summary.push(...report.changes.map((change) => `- #${change.number} ${describe(change)} (${change.reason})`));
  if (report.deferred.length) {
    summary.push("", `Changed shortly before or during the sweep, left to their own event run: ${issueList(report.deferred)}`);
  }
  if (report.untriaged.length) {
    summary.push("", `Open issues with no Type and nothing to derive one from: ${issueList(report.untriaged)}`);
  }
  if (report.stopped) {
    const left = report.left.length ? ` Not synced yet: ${issueList(report.left)}.` : "";
    summary.push("", `Stopped early: ${report.stopped}.${left}`);
  }
  if (report.failures.length) summary.push("", "Failures:", ...report.failures.map((failure) => `- ${failure}`));
  return summary.join("\n");
}

// An event run the rate limit stopped leaves its issue to the next sweep and
// passes; a sweep that stopped with work left fails, so someone re-runs it.
export function exitCode(report, { event }) {
  return report.failures.length > 0 || (!event && report.stopped !== null) ? 1 : 0;
}

async function main() {
  const repo = process.env.GITHUB_REPOSITORY;
  const client = createClient({ token: process.env.GH_TOKEN });
  const event = process.env.GITHUB_EVENT_NAME === "issues";
  const report = event
    ? await syncEvent(client, repo, JSON.parse(readFileSync(process.env.GITHUB_EVENT_PATH, "utf8")))
    : await sweep(client, repo, { days: parseSweepDays(process.env.SWEEP_DAYS) });

  for (const change of report.changes) console.log(`- #${change.number} ${describe(change)} (${change.reason})`);
  for (const failure of report.failures) console.error(failure);
  if (report.stopped) console.log(`::warning title=Issue type sync stopped early::${report.stopped}`);
  if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${summarize(report)}\n`);
  process.exitCode = exitCode(report, { event });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
