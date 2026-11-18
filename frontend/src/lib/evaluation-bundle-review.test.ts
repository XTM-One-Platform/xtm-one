// The STIX bundle review's pure part (#12282): the statuses, counts and verdicts follow the
// scorer's rules, near-misses are explained, mentions are found in the report text and the
// layouts place every object.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import type { BundleReview } from "@/types";
import {
  EXPECTED,
  NONE,
  answerName,
  buildModel,
  compareEntity,
  compareRelationship,
  derive,
  displayStatus,
  entityCounts,
  entityVerdict,
  entityVisible,
  f1,
  filterKey,
  fitBox,
  forceLayout,
  formatScore,
  graphLayout,
  locateMentions,
  modeOf,
  nearKind,
  relationshipCounts,
  tripleText,
  typeCounts,
  wordDiff,
  zoomBox,
} from "./evaluation-bundle-review.ts";

// APT29 (alias Cozy Bear) uses Cobalt Strike and Phishing (T1566).
// Answer "a" names the actor Cozy Bear (a near-miss) and adds a spurious domain;
// answer "b" names it right but misses Phishing.
const REVIEW: Pick<BundleReview, "objects" | "groups" | "sources" | "relationships"> = {
  objects: [
    { t: "intrusion-set", n: "APT29", c: "intrusion-set--1", a: ["Cozy Bear"] },
    { t: "tool", n: "Cobalt Strike", c: "tool--1" },
    { t: "attack-pattern", n: "Phishing", c: "attack-pattern--1", x: "T1566" },
    { t: "intrusion-set", n: "Cozy Bear", c: "intrusion-set--2" },
    { t: "domain-name", n: "evil[.]com", c: "domain-name--1" },
  ],
  groups: [0, 1, 2, 0, 3],
  sources: { [EXPECTED]: [0, 1, 2], a: [1, 3, 4], b: [0, 1] },
  relationships: [
    [0, 1, { [EXPECTED]: ["uses", 0, 1, 0], a: ["uses", 3, 1, 0], b: ["uses", 0, 1, 1] }],
    [0, 2, { [EXPECTED]: ["uses", 0, 2, 0] }],
  ],
};

const model = buildModel(REVIEW);
const [actor, tool, phishing, domain] = model.groups;

describe("buildModel", () => {
  it("groups each expected object with its near-misses, in reading order", () => {
    assert.equal(model.groups.length, 4);
    assert.deepEqual(model.types, ["intrusion-set", "tool", "attack-pattern", "domain-name"]);
    assert.equal(actor.by[EXPECTED].name, "APT29");
    assert.equal(actor.by.a.name, "Cozy Bear");
    assert.equal(domain.type, "domain-name");
    assert.equal(model.rels[0].dup.b, 1);
  });

  it("keeps an answer's object with the expected canonical id when it gave the group two", () => {
    const m = buildModel({ ...REVIEW, sources: { ...REVIEW.sources, c: [3, 0] } });
    assert.equal(m.groups[0].by.c.cid, "intrusion-set--1");
    assert.equal(m.groups[0].extra.c, 1);
  });

  it("reads an empty review", () => {
    const m = buildModel({ objects: [], groups: [], sources: { [EXPECTED]: [] }, relationships: [] });
    assert.deepEqual(m.groups, []);
    assert.deepEqual(m.types, []);
  });
});

describe("comparison", () => {
  it("tells a match, a near-miss, a miss and a spurious object apart", () => {
    assert.equal(compareEntity(actor, EXPECTED, "a"), "near");
    assert.equal(compareEntity(tool, EXPECTED, "a"), "tp");
    assert.equal(compareEntity(phishing, EXPECTED, "a"), "fn");
    assert.equal(compareEntity(domain, EXPECTED, "a"), "fp");
    assert.equal(compareEntity(domain, EXPECTED, "b"), null);
  });

  it("explains a near-miss", () => {
    assert.equal(nearKind(actor.by[EXPECTED], actor.by.a), "alias");
    const base = { cid: "x", type: "malware", aliases: [], ext: null, hashes: null };
    assert.equal(nearKind({ ...base, name: "Emotet" }, { ...base, name: "Emotet", type: "tool" }), "type");
    assert.equal(nearKind({ ...base, name: "evil.com" }, { ...base, name: "evil[.]com" }), "defanged");
    assert.equal(nearKind({ ...base, name: "Cobalt-Strike" }, { ...base, name: "cobalt strike" }), "case");
    assert.equal(nearKind({ ...base, name: "Emotet" }, { ...base, name: "Emotet loader" }), "length");
    assert.equal(nearKind({ ...base, name: "A", ext: "T1" }, { ...base, name: "B", ext: "T1" }), "attack-id");
    assert.equal(nearKind({ ...base, name: "Alpha" }, { ...base, name: "Omega" }), "wording");
  });

  it("compares relationships on their real endpoints, as the scorer's triples", () => {
    assert.deepEqual(compareRelationship(model, model.rels[0], EXPECTED, "a"), { st: "cascade", cause: actor.id });
    assert.deepEqual(compareRelationship(model, model.rels[0], EXPECTED, "b"), { st: "tp" });
    assert.deepEqual(compareRelationship(model, model.rels[1], EXPECTED, "b"), { st: "fn", cause: phishing.id });
  });
});

describe("counts", () => {
  it("counts a near-miss as one missing plus one spurious, unless the what-if counts it", () => {
    assert.deepEqual(entityCounts(model, "a", false), { tp: 1, fp: 2, fn: 2 });
    assert.deepEqual(entityCounts(model, "a", true), { tp: 2, fp: 1, fn: 1 });
    assert.equal(formatScore(f1(entityCounts(model, "a", false))), "33.3");
    assert.equal(formatScore(f1(entityCounts(model, "a", true))), "66.7");
  });

  it("counts a duplicate relationship as spurious", () => {
    assert.deepEqual(relationshipCounts(model, "b", false), { tp: 1, fp: 1, fn: 1 });
    assert.deepEqual(relationshipCounts(model, "a", false), { tp: 0, fp: 1, fn: 2 });
  });

  it("counts per type", () => {
    assert.deepEqual(typeCounts(model, "b", false, "attack-pattern"), { tp: 0, fp: 0, fn: 1 });
    assert.equal(f1({ tp: 0, fp: 0, fn: 0 }), 100);
  });

  it("formats a missing score as a dash", () => {
    assert.equal(formatScore(null), "—");
    assert.equal(formatScore(72.345), "72.3");
  });
});

describe("pairs", () => {
  it("knows its mode", () => {
    assert.equal(modeOf({ ref: EXPECTED, under: "a" }), "score");
    assert.equal(modeOf({ ref: NONE, under: "a" }), "alone");
    assert.equal(modeOf({ ref: "a", under: "b" }), "runs");
  });

  it("says what changed from one answer to the next", () => {
    assert.equal(entityVerdict(actor, "a", "b", false), "fixed");
    assert.equal(entityVerdict(domain, "a", "b", false), "fixed");
    // Missed by both: on neither side, so not in the comparison.
    assert.equal(entityVerdict(phishing, "a", "b", false), null);
    assert.equal(entityVerdict(tool, "a", "b", false), "same");
    assert.equal(entityVerdict(actor, "b", "a", false), "regressed");
    assert.equal(entityVerdict(domain, "b", "a", false), "new");
    // The what-if counts the near-miss: nothing changes for the actor.
    assert.equal(entityVerdict(actor, "a", "b", true), "same");
  });

  it("derives one item per object and relationship on either side", () => {
    const d = derive(model, { ref: "a", under: "b" }, false);
    assert.deepEqual(d.entItems.map((it) => it.st), ["fixed", "same", "fixed"]);
    assert.deepEqual(d.relItems.map((it) => [it.st, it.pairSt]), [["fixed", "cascade"]]);
    const alone = derive(model, { ref: NONE, under: "a" }, false);
    assert.equal(alone.entItems.length, 3);
    assert.ok(alone.entItems.every((it) => it.a === null));
    const score = derive(model, { ref: EXPECTED, under: "b" }, false);
    assert.deepEqual(score.relItems.map((it) => it.st), ["tp", "fn"]);
  });

  it("shows a near-miss as matched in the what-if, and filters it as a near-miss", () => {
    assert.equal(displayStatus("near", "score", true), "sim");
    assert.equal(displayStatus("near", "score", false), "near");
    assert.equal(displayStatus("fixed", "runs", true), "fixed");
    assert.equal(filterKey("cascade", "score"), "near");
    const d = derive(model, { ref: EXPECTED, under: "a" }, false);
    const it = d.ents.get(actor.id);
    assert.equal(entityVisible(it, { type: null, hidden: new Set() }, "score"), true);
    assert.equal(entityVisible(it, { type: null, hidden: new Set(["near"]) }, "score"), false);
    assert.equal(entityVisible(it, { type: "tool", hidden: new Set() }, "score"), false);
  });
});

describe("mentions", () => {
  it("finds names, aliases, ATT&CK ids, refanged values and plurals, whole words only", () => {
    const text = "APT29, also known as Cozy Bear, used Cobalt Strikes. Phishing (T1566) via evil.com; not APT290.";
    const { pages, mentions } = locateMentions(model, [text]);
    const found = pages[0].filter((s) => typeof s !== "string");
    assert.deepEqual(
      found.map((m) => [m.text, m.how]),
      [["APT29", "name"], ["Cozy Bear", "alias"], ["Cobalt Strikes", "plural"], ["Phishing", "name"], ["T1566", "id"], ["evil.com", "defang"]],
    );
    assert.equal(mentions.get(actor.id)?.length, 2);
    assert.equal(pages[0].map((s) => (typeof s === "string" ? s : s.text)).join(""), text);
    const alias = found.find((m) => m.text === "Cozy Bear");
    assert.deepEqual(alias?.srcs.sort(), ["a", EXPECTED, "b"].sort());
  });

  it("leaves a page without mentions as plain text", () => {
    const { pages, mentions } = locateMentions(model, ["Nothing here."]);
    assert.deepEqual(pages, [["Nothing here."]]);
    assert.equal(mentions.size, 0);
  });
});

describe("layouts and text", () => {
  it("places every group of the knowledge graph, fast on a large entry", () => {
    const pos = forceLayout(model);
    assert.equal(pos.size, model.groups.length);
    for (const p of pos.values()) assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y));
    const n = 280;
    const big = buildModel({
      objects: Array.from({ length: n }, (_, i) => ({ t: "malware", n: `m${i}`, c: `malware--${i}` })),
      groups: Array.from({ length: n }, (_, i) => i),
      sources: { [EXPECTED]: Array.from({ length: n }, (_, i) => i) },
      relationships: Array.from({ length: n - 1 }, (_, i) => [i, i + 1, { [EXPECTED]: ["uses", i, i + 1, 0] }]),
    });
    const t0 = performance.now();
    assert.equal(forceLayout(big).size, n);
    assert.ok(performance.now() - t0 < 3000);
  });

  it("lays the graph out in columns and fits and zooms a box", () => {
    const { pos, height } = graphLayout(model.groups);
    assert.equal(pos.get(actor.id)?.x, 292);
    assert.equal(pos.get(domain.id)?.x, 745);
    assert.ok(height >= 560);
    const box = fitBox([{ x: 0, y: 0 }, { x: 100, y: 50 }]);
    assert.deepEqual(box, { x: -90, y: -60, w: 280, h: 190 });
    assert.deepEqual(zoomBox({ x: 0, y: 0, w: 100, h: 100 }, 0.5), { x: 25, y: 25, w: 50, h: 50 });
  });

  it("writes triples and word diffs", () => {
    assert.equal(tripleText(model.rels[0], "a"), "Cozy Bear → uses → Cobalt Strike");
    assert.equal(tripleText(model.rels[1], "a"), null);
    assert.deepEqual(wordDiff("APT 29 group", "APT 28 group"), { a: ["APT 2", "9", " group"], b: ["APT 2", "8", " group"] });
  });
});

describe("answerName", () => {
  const model = (id: string) => id.toUpperCase();
  const a = { evaluation_id: "e1", evaluation_name: "STIX", subject_name: "Harvester", model_id: "m" };
  const b = { evaluation_id: "e1", evaluation_name: "STIX", subject_name: "Grounded", model_id: "m" };
  it("names answers of one evaluation by their agent", () => {
    assert.deepEqual(answerName([a, b], b, model), { title: "Grounded", detail: "M" });
  });
  it("names answers of several evaluations by their evaluation, the agent in the detail", () => {
    const other = { ...b, evaluation_id: "e2", evaluation_name: "Golden" };
    assert.deepEqual(answerName([a, other], other, model), { title: "Golden", detail: "Grounded · M" });
  });
  it("falls back on the evaluation when the agent is unknown", () => {
    const flowless = { ...a, subject_name: null };
    assert.deepEqual(answerName([flowless], flowless, model), { title: "STIX", detail: "M" });
  });
});
