import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import * as sync from "./issue-type-sync.mjs";
import { hasChanges, plan, titleKind } from "./issue-type-sync.mjs";

const issue = (title, labels = [], type = null) => ({ title, labels, type });
const changes = (change) => ({ add: change.add, remove: change.remove, type: change.type });
const readGithubFile = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("every Conventional Commits type maps to its kind", () => {
  assert.equal(titleKind("feat(chat): stream answers"), "feature");
  assert.equal(titleKind("feat(api)!: drop the v1 endpoints"), "feature");
  assert.equal(titleKind("fix: handle a missing token"), "bug");
  assert.equal(titleKind("bug: legacy prefix"), "bug");
  assert.equal(titleKind("docs(licensing): name the boundary"), "documentation");
  for (const prefix of ["chore", "test", "refactor", "ci", "perf", "style", "build", "revert"]) {
    assert.equal(titleKind(`${prefix}(scope): something`), "task", prefix);
  }
  assert.equal(titleKind("Webhook assignments execute twice"), null);
  assert.equal(titleKind("RunBudget: overshoot"), null);
  assert.equal(titleKind("[Bug] evaluations filter"), null);
});

test("the title prefix wins over the labels and the Type", () => {
  assert.deepEqual(changes(plan(issue("feat(worker): drop cluster support", ["bug", "backend"], "Bug"))), {
    add: ["feature"],
    remove: ["bug"],
    type: "Feature",
  });
  assert.deepEqual(changes(plan(issue("docs(licensing): name the boundary", ["bug", "needs triage"]))), {
    add: ["documentation"],
    remove: ["bug"],
    type: "Task",
  });
  assert.deepEqual(changes(plan(issue("chore(a2a): runbook", ["documentation", "chore"], "Feature"))), {
    add: [],
    remove: ["documentation"],
    type: "Task",
  });
});

test("an issue created from the CLI with a label gets its Type", () => {
  assert.deepEqual(changes(plan(issue("fix(agent-loop): read UTF-16 JSON", ["bug", "filigran team"]))), {
    add: [],
    remove: [],
    type: "Bug",
  });
});

test("a consistent issue needs no change", () => {
  for (const consistent of [
    issue("fix: something", ["bug"], "Bug"),
    issue("feat: something", ["feature", "ui"], "Feature"),
    issue("docs: something", ["documentation"], "Task"),
    issue("refactor: something", ["refactor"], "Task"),
    issue("Legacy free text", ["bug"], "Bug"),
    issue("Legacy free text", [], "Task"),
  ]) {
    assert.equal(hasChanges(plan(consistent)), false, consistent.title);
  }
});

test("without a prefix, the triggering change wins", () => {
  const retyped = issue("Webhook assignments execute twice", ["bug"], "Task");
  assert.deepEqual(changes(plan(retyped, { action: "typed" })), { add: [], remove: ["bug"], type: null });
  assert.deepEqual(changes(plan(retyped)), { add: [], remove: [], type: "Bug" });

  const relabelled = issue("Webhook assignments execute twice", ["bug"], "Feature");
  assert.deepEqual(changes(plan(relabelled, { action: "labeled", label: "bug" })), { add: [], remove: [], type: "Bug" });
});

test("a label event is ignored once the label is gone again", () => {
  const change = plan(issue("Legacy free text", [], "Feature"), { action: "labeled", label: "bug" });
  assert.deepEqual(changes(change), { add: ["feature"], remove: [], type: null });
});

test("deprecated labels are replaced by feature", () => {
  assert.deepEqual(changes(plan(issue("feat(kb): offer HTML ingestion", ["enhancement"], "Feature"))), {
    add: ["feature"],
    remove: ["enhancement"],
    type: null,
  });
  assert.deepEqual(changes(plan(issue("Support pagination", ["feature-request"]))), {
    add: ["feature"],
    remove: ["feature-request"],
    type: "Feature",
  });
  assert.deepEqual(changes(plan(issue("perf(traces): memoize", ["enhancement"]))), {
    add: [],
    remove: ["enhancement"],
    type: "Task",
  });
});

test("a security title never gets the security label, and an Aikido finding is a bug", () => {
  // The security label is retired (see security-label-guard.yml): the sync never adds it.
  assert.deepEqual(plan(issue("fix(security): restrict access", ["bug"], "Bug")).add, []);
  assert.deepEqual(plan(issue("security: IDOR in several endpoints", ["bug"], "Bug")).add, []);
  assert.deepEqual(changes(plan(issue("Path traversal attack possible - xtm-one", ["Aikido", "critical"]))), {
    add: ["bug"],
    remove: [],
    type: "Bug",
  });
});

test("an issue with nothing to go on, or a custom Type, is left alone", () => {
  const unknown = plan(issue("Question about deployment", ["question", "needs triage"]));
  assert.equal(unknown.kind, null);
  assert.equal(hasChanges(unknown), false);
  assert.equal(hasChanges(plan(issue("fix: something", [], "Epic"))), false);
});

test("a prefix is read whatever its case, spacing, scope or breaking mark", () => {
  assert.equal(titleKind("  FEAT(Chat)!: stream answers"), "feature");
  assert.equal(titleKind("feat!: drop the v1 endpoints"), "feature");
  assert.equal(titleKind("Fix : handle a missing token"), "bug");
  assert.equal(titleKind("BUG: legacy prefix"), "bug");
  assert.equal(titleKind("\tDocs(a2a): runbook"), "documentation");
  for (const notAPrefix of ["Feature: pagination", "Bugfix: crash", "fixed: typo", "ci/cd: pipeline", "feat - no colon"]) {
    assert.equal(titleKind(notAPrefix), null, notAPrefix);
  }
});

test("the discontinued bracket titles have no prefix and fall back to the labels, then the Type", () => {
  for (const title of ["[backend] Fix the token refresh", "[frontend] feat: dark mode", "[Bug] evaluations filter"]) {
    assert.equal(titleKind(title), null, title);
  }
  assert.deepEqual(changes(plan(issue("[backend] Fix the token refresh", ["bug"]))), { add: [], remove: [], type: "Bug" });
  assert.deepEqual(changes(plan(issue("[frontend] Dark mode", [], "Feature"))), { add: ["feature"], remove: [], type: null });
});

test("chore is an area label: never added, never removed, never read as a type", () => {
  // A `ci:` issue carrying `chore`, typed Task.
  const ci = "ci: keep issue types and type labels in sync with the title prefix";
  assert.equal(hasChanges(plan(issue(ci, ["filigran team", "chore"], "Task"))), false);
  assert.deepEqual(changes(plan(issue(ci, ["filigran team", "chore"]))), { add: [], remove: [], type: "Task" });
  assert.deepEqual(changes(plan(issue("feat: offer HTML ingestion", ["chore"], "Task"))), {
    add: ["feature"],
    remove: [],
    type: "Feature",
  });
  assert.equal(plan(issue("Housekeeping", ["chore"])).kind, null);
});

// Reads .github/LABELS.md itself, so a change to the convention fails here
// until the rules follow it.
function labelsConvention() {
  const doc = readGithubFile("LABELS.md");
  const section = (heading) => {
    const start = doc.indexOf(heading);
    assert.notEqual(start, -1, `LABELS.md has no "${heading}" section`);
    const end = doc.indexOf("\n## ", start + heading.length);
    return doc.slice(start, end === -1 ? undefined : end);
  };
  const backticked = (text) => [...text.matchAll(/`([^`]+)`/g)].map((match) => match[1]);

  const typeList = /`type` is one of:([\s\S]*?)\.\s*\n/.exec(section("## 1. Title convention"));
  assert.ok(typeList, "LABELS.md section 1 no longer lists the title types");
  const types = backticked(typeList[1]);

  const typeSection = section("## 2. Type label");
  const typeLabels = Object.fromEntries(
    [...typeSection.matchAll(/^\|\s*`([a-z]+):`\s*\|\s*`([a-z-]+)`\s*\|/gm)].map((match) => [match[1], match[2]]),
  );
  const typeRule = /\*\*Type\*\* field to match \(([\s\S]*?)\)\./.exec(typeSection);
  assert.ok(typeRule, "LABELS.md section 2 no longer states the GitHub Type rule");
  const githubTypes = Object.fromEntries(
    [...typeRule[1].matchAll(/`([a-z]+):`\s+\S+\s+`([A-Za-z]+)`/g)].map((match) => [match[1], match[2]]),
  );
  const otherType = /every other type\s+\S+\s+`([A-Za-z]+)`/.exec(typeRule[1])?.[1];

  const deprecated = [];
  for (const line of section("## 5. Deprecated labels").split("\n").filter((l) => l.startsWith("- "))) {
    const [labels, replacement] = line.split(/\s+use\s+/);
    assert.ok(replacement?.startsWith("`feature`"), `LABELS.md no longer replaces ${labels} with feature`);
    deprecated.push(...backticked(labels));
  }
  return { types, typeLabels, githubTypes, otherType, deprecated };
}

test("the rules say exactly what LABELS.md says", () => {
  const convention = labelsConvention();
  assert.equal(convention.types.length, 11);
  assert.deepEqual(Object.keys(convention.typeLabels).sort(), ["docs", "feat", "fix"]);
  assert.equal(convention.otherType, "Task");

  // Every Conventional Commits type, plus the legacy `bug:` that means `fix:`.
  assert.deepEqual(
    Object.keys(sync.PREFIX_KINDS)
      .filter((prefix) => prefix !== "bug")
      .sort(),
    [...convention.types].sort(),
  );
  assert.equal(sync.PREFIX_KINDS.bug, sync.PREFIX_KINDS.fix);
  for (const prefix of convention.types) {
    const target = sync.KINDS[sync.PREFIX_KINDS[prefix]];
    assert.equal(target.label, convention.typeLabels[prefix] ?? null, `${prefix}: type label`);
    assert.equal(target.type, convention.githubTypes[prefix] ?? convention.otherType, `${prefix}: GitHub Type`);
  }
  assert.deepEqual([...sync.TYPE_LABELS].sort(), Object.values(convention.typeLabels).sort());
  assert.deepEqual([...sync.DEPRECATED_LABELS].sort(), [...convention.deprecated].sort());
});

test("the workflow starts a run for exactly the labels and actions the rules read", () => {
  const workflow = readGithubFile("workflows/issue-type-sync.yml");
  const allowList = (subject) => {
    const match = new RegExp(`contains\\(fromJSON\\('(\\[[^']*\\])'\\),\\s*${subject.replaceAll(".", "\\.")}\\)`).exec(workflow);
    assert.ok(match, `the workflow has no allow-list for ${subject}`);
    return JSON.parse(match[1]);
  };

  for (const label of [...sync.TYPE_LABELS, ...sync.DEPRECATED_LABELS, "Aikido"]) {
    assert.ok(sync.WATCHED_LABELS.includes(label), label);
  }
  const labels = allowList("github.event.label.name");
  assert.equal(new Set(labels).size, labels.length, "a label is listed twice");
  assert.deepEqual([...labels].sort(), [...sync.WATCHED_LABELS].sort());

  // `edited` runs for a title change only and `labeled` / `unlabeled` for the
  // watched labels only; every other activity type the workflow listens to
  // starts a run unconditionally.
  const activityTypes = /^\s+issues:\s*\n\s+types:\s*\[([^\]]*)\]/m.exec(workflow);
  assert.ok(activityTypes, "the workflow no longer lists its issue activity types");
  assert.deepEqual(
    [...allowList("github.event.action"), "edited", "labeled", "unlabeled"].sort(),
    activityTypes[1]
      .split(",")
      .map((type) => type.trim())
      .sort(),
  );
});

test("a synced issue needs nothing more, and only type and deprecated labels are ever removed", () => {
  const titles = [
    "feat: a",
    "fix(api)!: b",
    "bug: c",
    "docs: d",
    "chore: e",
    "ci: f",
    "refactor(x): g",
    "Free text",
    "[backend] Legacy bracket",
    "security: h",
    "perf(security): i",
  ];
  const pool = ["bug", "feature", "documentation", "enhancement", "feature request", "Aikido", "security", "chore"];
  const events = [
    {},
    { action: "typed" },
    { action: "labeled", label: "bug" },
    { action: "labeled", label: "feature" },
    { action: "labeled", label: "documentation" },
    { action: "labeled", label: "enhancement" },
  ];
  const removable = new Set([...sync.TYPE_LABELS, ...sync.DEPRECATED_LABELS]);
  for (const title of titles) {
    for (let mask = 0; mask < 1 << pool.length; mask++) {
      const labels = pool.filter((_, bit) => mask & (1 << bit));
      for (const type of [null, "Task", "Bug", "Feature", "Epic"]) {
        for (const event of events) {
          const before = issue(title, labels, type);
          const change = plan(before, event);
          const where = JSON.stringify({ before, event, change: changes(change) });
          assert.ok(change.add.every((label) => !labels.includes(label)), `adds a label it has: ${where}`);
          assert.ok(change.remove.every((label) => labels.includes(label) && removable.has(label)), `removes: ${where}`);
          if (type === "Epic") assert.equal(hasChanges(change), false, `touches a custom Type: ${where}`);
          const labelsAfter = [...labels.filter((label) => !change.remove.includes(label)), ...change.add];
          const after = issue(title, labelsAfter, change.type ?? type);
          assert.equal(hasChanges(plan(after)), false, `a second run changes it again: ${where}`);
        }
      }
    }
  }
});

test("the sweep window must be a whole number of days", () => {
  assert.equal(sync.parseSweepDays(undefined), 3);
  assert.equal(sync.parseSweepDays(""), 3);
  assert.equal(sync.parseSweepDays(" "), 3);
  assert.equal(sync.parseSweepDays("0"), 0);
  assert.equal(sync.parseSweepDays(" 7 "), 7);
  for (const bad of ["abc", "-1", "1.5", "1e3", "3 days"]) {
    assert.throws(() => sync.parseSweepDays(bad), /whole number of days/, bad);
  }
});

test("a rate limit is told apart from a permission error, and its wait read from the headers", () => {
  const now = Date.parse("2026-09-26T12:00:00Z");
  const wait = (status, headers, text = "") => sync.rateLimitWait(status, new Headers(headers), text, now);
  assert.equal(wait(429, { "retry-after": "30" }), 30_000);
  assert.equal(wait(403, { "retry-after": "5" }, "You have exceeded a secondary rate limit"), 5_000);
  assert.equal(wait(403, { "x-ratelimit-remaining": "0", "x-ratelimit-reset": String(now / 1000 + 600) }), 601_000);
  assert.equal(wait(403, { "x-ratelimit-remaining": "12" }, "You have exceeded a secondary rate limit"), 60_000);
  assert.equal(wait(429, {}), 60_000);
  assert.equal(wait(403, { "x-ratelimit-remaining": "999" }, "Resource not accessible by integration"), null);
  assert.equal(wait(404, {}), null);
});

// A GitHub that keeps its issues in memory and records every request. An
// `intercept(call)` hook can answer a request itself, or change the store
// before the request is served (a person editing the issue meanwhile).
const REPO = "acme/widgets";
const T0 = Date.parse("2026-09-26T04:23:00Z");
const LISTED_AT = "2026-09-20T10:00:00Z";

function fakeGitHub({ issues = [], pulls = [], limit = 1000, remaining = 900, dropTypes = false, intercept } = {}) {
  const store = new Map(
    issues.map((item) => [item.number, { state: "open", type: null, updated_at: LISTED_AT, ...item, labels: [...(item.labels ?? [])] }]),
  );
  const calls = [];
  let budget = remaining;
  let clock = T0;
  const respond = (status, body, headers = {}) => {
    budget = Math.max(0, budget - 1);
    return new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: {
        "x-ratelimit-limit": String(limit),
        "x-ratelimit-remaining": String(budget),
        "x-ratelimit-reset": String(T0 / 1000 + 3600),
        ...headers,
      },
    });
  };
  const toRaw = (item) => ({ ...item, labels: item.labels.map((name) => ({ name })), type: item.type ? { name: item.type } : null });
  const touch = (item) => {
    item.updated_at = new Date((clock += 1000)).toISOString();
  };

  async function fetch(url, init = {}) {
    const { pathname, search } = new URL(url);
    const call = { method: init.method ?? "GET", path: pathname + search, body: init.body ? JSON.parse(init.body) : undefined };
    calls.push(call);
    const answer = intercept?.(call, { store, respond });
    if (answer) return answer;
    const match = /^\/repos\/acme\/widgets\/issues(?:\/(\d+)(\/labels(?:\/(.+))?)?)?$/.exec(pathname);
    if (!match) return respond(404, { message: "Not Found" });
    const [, number, labelsPath, label] = match;
    if (!number) return respond(200, [...[...store.values()].map(toRaw), ...pulls]);
    const item = store.get(Number(number));
    if (!item) return respond(404, { message: "Not Found" });
    if (call.method === "GET" && !labelsPath) return respond(200, toRaw(item));
    if (call.method === "POST" && labelsPath && !label) {
      for (const name of call.body.labels) if (!item.labels.includes(name)) item.labels.push(name);
      touch(item);
      return respond(200, item.labels.map((name) => ({ name })));
    }
    if (call.method === "DELETE" && label) {
      const name = decodeURIComponent(label);
      if (!item.labels.includes(name)) return respond(404, { message: "Label does not exist" });
      item.labels = item.labels.filter((existing) => existing !== name);
      touch(item);
      return respond(200, item.labels.map((existing) => ({ name: existing })));
    }
    if (call.method === "PATCH" && !labelsPath) {
      if (!dropTypes) {
        item.type = call.body.type;
        touch(item);
      }
      return respond(200, toRaw(item));
    }
    return respond(405, { message: "Method Not Allowed" });
  }

  const writes = () => calls.filter((call) => call.method !== "GET");
  const reads = (number) => calls.filter((call) => call.method === "GET" && call.path === `/repos/${REPO}/issues/${number}`);
  return { fetch, calls, store, writes, reads };
}

// A clock that only moves when the code under test sleeps.
function fakeTime() {
  const time = { now: T0, slept: [] };
  time.clock = () => time.now;
  time.sleep = async (ms) => {
    time.slept.push(ms);
    time.now += ms;
  };
  return time;
}

function run(github, time, days = 3, options = {}) {
  const client = sync.createClient({ token: "test-token", fetch: github.fetch, now: time.clock });
  return sync.sweep(client, REPO, { days, now: time.clock, sleep: time.sleep, ...options });
}

test("the sweep skips pull requests and re-reads only the issues that need a change", async () => {
  const github = fakeGitHub({
    issues: [
      { number: 1, title: "fix: consistent", labels: ["bug"], type: "Bug" },
      { number: 2, title: "feat: mislabelled", labels: ["bug"] },
      { number: 3, title: "Question about deployment", labels: ["question"] },
      { number: 4, title: "docs: closed without its label", state: "closed", type: "Task" },
    ],
    pulls: [{ number: 5, title: "feat: a pull request", labels: [], type: null, pull_request: {}, updated_at: LISTED_AT }],
  });
  const time = fakeTime();
  const report = await run(github, time);

  const [list] = github.calls;
  assert.match(list.path, /[?&]state=all(&|$)/);
  assert.match(list.path, /[?&]sort=created(&|$)/);
  assert.match(list.path, /[?&]since=2026-09-23T04%3A23%3A00.000Z(&|$)/);
  for (const untouched of [1, 3, 5]) assert.equal(github.reads(untouched).length, 0, `#${untouched} was re-read`);
  assert.deepEqual(github.store.get(2).labels, ["feature"]);
  assert.equal(github.store.get(2).type, "Feature");
  assert.deepEqual(github.store.get(4).labels, ["documentation"]);
  assert.deepEqual(
    report.changes.map((change) => change.number),
    [2, 4],
  );
  assert.deepEqual(report.untriaged, [3]);
  assert.deepEqual(report.failures, []);
  assert.equal(report.stopped, null);
  // One write a second on average: #2 took three, #4 one.
  assert.deepEqual(time.slept, [3000, 1000]);
  assert.equal(sync.exitCode(report, { event: false }), 0);

  const again = await run(github, time);
  assert.deepEqual(again.changes, []);
});

test("a full sweep lists every issue, whatever its age", async () => {
  const github = fakeGitHub({ issues: [{ number: 1, title: "fix: x", labels: ["bug"], type: "Bug" }] });
  await run(github, fakeTime(), 0);
  assert.doesNotMatch(github.calls[0].path, /since=/);
});

test("the sweep leaves an issue changed while it runs to that issue's own event run", async () => {
  const github = fakeGitHub({
    issues: [
      { number: 7, title: "fix: retitled while the sweep runs", labels: ["feature"], type: "Feature" },
      { number: 8, title: "fix: edited a minute before the sweep", labels: [], updated_at: "2026-09-26T04:22:00Z" },
    ],
    intercept(call, { store }) {
      if (call.method === "GET" && call.path === `/repos/${REPO}/issues/7`) {
        Object.assign(store.get(7), { title: "feat: retitled while the sweep runs", updated_at: "2026-09-26T04:23:01Z" });
      }
    },
  });
  const report = await run(github, fakeTime());

  assert.deepEqual(github.writes(), []);
  assert.equal(github.reads(8).length, 0);
  assert.deepEqual(report.deferred.sort(), [7, 8]);
  assert.deepEqual(report.changes, []);
});

test("a short rate limit is waited out and the issue synced again from a fresh read", async () => {
  let limited = 0;
  const github = fakeGitHub({
    issues: [{ number: 9, title: "fix: rate limited once", labels: [] }],
    intercept(call, { respond }) {
      if (call.method === "POST" && limited++ === 0) {
        return respond(403, { message: "You have exceeded a secondary rate limit." }, { "retry-after": "2" });
      }
      if (call.method === "GET" && call.path.includes("?") && github.calls.length === 1) {
        return respond(502, { message: "Bad Gateway" });
      }
    },
  });
  const time = fakeTime();
  const report = await run(github, time);

  assert.deepEqual(time.slept.slice(0, 2), [5000, 2000]);
  assert.equal(github.reads(9).length, 2);
  assert.deepEqual(github.store.get(9).labels, ["bug"]);
  assert.equal(github.store.get(9).type, "Bug");
  assert.deepEqual(
    report.changes.map((change) => change.number),
    [9],
  );
  assert.equal(report.stopped, null);
});

test("an exhausted rate limit stops the sweep, which reports what it left", async () => {
  const github = fakeGitHub({
    issues: [
      { number: 10, title: "fix: first", labels: [] },
      { number: 11, title: "feat: second", labels: [] },
      { number: 12, title: "docs: third", labels: [] },
    ],
    intercept(call, { respond }) {
      if (call.method === "PATCH") {
        return respond(403, { message: "API rate limit exceeded" }, {
          "x-ratelimit-remaining": "0",
          "x-ratelimit-reset": String(T0 / 1000 + 2400),
        });
      }
    },
  });
  const time = fakeTime();
  const report = await run(github, time);

  assert.equal(github.calls.at(-1).method, "PATCH", "no request after the rate limit");
  assert.deepEqual(time.slept, [], "a wait of 40 minutes is not sat through");
  assert.match(report.stopped, /rate limit/);
  assert.deepEqual(report.left, [10, 11, 12]);
  // #10 got its label before the limit hit; its Type is still owed.
  assert.deepEqual(report.changes, [{ number: 10, reason: "title prefix", add: ["bug"], remove: [], type: null }]);
  assert.equal(sync.exitCode(report, { event: false }), 1);
  assert.match(sync.summarize(report), /Not synced yet: #10, #11, #12/);
});

test("the sweep keeps part of the hourly API budget for the other workflows", async () => {
  const github = fakeGitHub({
    issues: [
      { number: 13, title: "fix: first", labels: [] },
      { number: 14, title: "feat: second", labels: [] },
    ],
    remaining: 150,
  });
  const report = await run(github, fakeTime());

  assert.equal(github.calls.length, 1, "only the list was read");
  assert.match(report.stopped, /budget/);
  assert.deepEqual(report.left, [13, 14]);
});

test("a Type GitHub silently drops fails that issue, and the sweep carries on", async () => {
  const github = fakeGitHub({
    issues: [
      { number: 15, title: "fix: typed by a token without push access", labels: [] },
      { number: 16, title: "feat: only needs its label", labels: [], type: "Feature" },
    ],
    dropTypes: true,
  });
  const report = await run(github, fakeTime());

  assert.equal(report.failures.length, 1);
  assert.match(report.failures[0], /^#15: .*dropped the Type change to Bug/);
  assert.deepEqual(github.store.get(16).labels, ["feature"]);
  assert.equal(sync.exitCode(report, { event: false }), 1);
});

test("an issue deleted or transferred meanwhile is skipped, not failed", async () => {
  const github = fakeGitHub({
    issues: [
      { number: 17, title: "fix: deleted meanwhile", labels: [] },
      { number: 18, title: "fix: transferred meanwhile", labels: [] },
    ],
    intercept(call, { respond }) {
      if (call.path === `/repos/${REPO}/issues/17`) return respond(410, { message: "This issue was deleted" });
      if (call.path === `/repos/${REPO}/issues/18`) {
        return respond(301, { message: "Moved Permanently" }, { location: "https://api.github.com/repositories/1/issues/2" });
      }
    },
  });
  const report = await run(github, fakeTime());

  assert.deepEqual(github.writes(), []);
  assert.deepEqual(report.failures, []);
  assert.deepEqual(report.changes, []);
});

test("a label someone else removed meanwhile is not reported as removed", async () => {
  const github = fakeGitHub({
    issues: [{ number: 19, title: "feat: relabelled twice", labels: ["bug"], type: "Feature" }],
    intercept(call, { store }) {
      if (call.method === "DELETE") store.get(19).labels = store.get(19).labels.filter((label) => label !== "bug");
    },
  });
  const report = await run(github, fakeTime());

  assert.deepEqual(github.store.get(19).labels, ["feature"]);
  assert.deepEqual(report.changes, [{ number: 19, reason: "title prefix", add: ["feature"], remove: [], type: null }]);
  assert.deepEqual(report.failures, []);
});

test("an event run syncs its issue, the triggering change first", async () => {
  const github = fakeGitHub({ issues: [{ number: 20, title: "Webhook assignments execute twice", labels: ["bug"], type: "Feature" }] });
  const time = fakeTime();
  const client = sync.createClient({ token: "test-token", fetch: github.fetch, now: time.clock });
  const payload = { action: "labeled", label: { name: "bug" }, issue: { number: 20 } };
  const report = await sync.syncEvent(client, REPO, payload, { now: time.clock, sleep: time.sleep });

  assert.equal(github.store.get(20).type, "Bug");
  assert.deepEqual(report.changes, [{ number: 20, reason: 'label "bug" added', add: [], remove: [], type: "Bug" }]);
  assert.equal(sync.exitCode(report, { event: true }), 0);
});

test("an event run the rate limit stops leaves its issue to the next sweep", async () => {
  const github = fakeGitHub({
    issues: [{ number: 21, title: "fix: x", labels: [] }],
    intercept: (call, { respond }) => respond(429, { message: "Too Many Requests" }, { "retry-after": "3600" }),
  });
  const time = fakeTime();
  const client = sync.createClient({ token: "test-token", fetch: github.fetch, now: time.clock });
  const report = await sync.syncEvent(client, REPO, { action: "opened", issue: { number: 21 } }, { now: time.clock, sleep: time.sleep });

  assert.equal(github.calls.length, 1);
  assert.match(report.stopped, /next sweep/);
  assert.deepEqual(report.left, [21]);
  assert.equal(sync.exitCode(report, { event: true }), 0);
});

test("an event run fails loudly when GitHub drops the Type, and does nothing for a deleted issue", async () => {
  const time = fakeTime();
  const dropping = fakeGitHub({ issues: [{ number: 22, title: "fix: x", labels: ["bug"] }], dropTypes: true });
  const client = sync.createClient({ token: "test-token", fetch: dropping.fetch, now: time.clock });
  await assert.rejects(
    sync.syncEvent(client, REPO, { action: "opened", issue: { number: 22 } }, { now: time.clock, sleep: time.sleep }),
    /dropped the Type change to Bug - the token needs push access/,
  );

  const empty = fakeGitHub();
  const report = await sync.syncEvent(
    sync.createClient({ token: "test-token", fetch: empty.fetch, now: time.clock }),
    REPO,
    { action: "edited", issue: { number: 23 } },
    { now: time.clock, sleep: time.sleep },
  );
  assert.deepEqual(empty.writes(), []);
  assert.deepEqual(report.changes, []);
});
