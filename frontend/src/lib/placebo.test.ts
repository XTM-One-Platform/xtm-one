/**
 * Unit tests for the placebo-controlled evaluation helpers - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  NEW_PLACEBO_DRAFT,
  configFromDraft,
  draftFromConfig,
  formatDeviation,
  formatDifference,
  formatRatio,
  formatShare,
  isComparisonRun,
  isControlArm,
  placeboDraftError,
  placeboWarningLabel,
  shortCorpusVersion,
  targetFromKey,
} from "./placebo.ts";

const draft = (patch: Partial<typeof NEW_PLACEBO_DRAFT>) => ({ ...NEW_PLACEBO_DRAFT, ...patch });

test("a configuration round-trips through its draft, targets in the server's order", () => {
  const config = {
    targets: [
      { kind: "persona" as const, skill_id: null },
      { kind: "prompts" as const, skill_id: null },
      { kind: "skill" as const, skill_id: "s-1" },
      { kind: "skill" as const, skill_id: "s-2" },
    ],
    include_no_target: false,
    trials_per_entry: 7,
    custom_text: "Some text",
  };

  assert.deepEqual(configFromDraft(draftFromConfig(config)), config);
});

test("no configuration drafts the default: the persona, five trials, a no-target arm", () => {
  assert.deepEqual(configFromDraft(draftFromConfig(null)), {
    targets: [{ kind: "persona", skill_id: null }],
    include_no_target: true,
    trials_per_entry: 5,
    custom_text: null,
  });
});

test("a blank custom text is sent as none", () => {
  assert.equal(configFromDraft(draft({ customText: "  \n " })).custom_text, null);
});

test("each rule the server enforces is checked before sending", () => {
  const cases: [Partial<typeof NEW_PLACEBO_DRAFT>, string, string | null][] = [
    [{}, "llm_only", null],
    [{ persona: false }, "full_loop", "no_target"],
    [{ skills: true, skillIds: ["s-1"] }, "full_loop", "skills_and_skill"],
    [{ skillIds: Array.from({ length: 20 }, (_, i) => `s-${i}`) }, "full_loop", "too_many_targets"],
    [{ trialsPerEntry: 0 }, "full_loop", "trials"],
    [{ trialsPerEntry: 31 }, "full_loop", "trials"],
    [{ trialsPerEntry: 2.5 }, "full_loop", "trials"],
    [{ trialsPerEntry: 30 }, "full_loop", null],
    [{ customText: "x".repeat(20_001) }, "full_loop", "custom_text"],
    [{ prompts: true }, "llm_only", "needs_full_loop"],
    [{ skillIds: ["s-1"] }, "llm_only", "needs_full_loop"],
    [{ prompts: true, skills: true }, "full_loop", null],
  ];
  for (const [patch, mode, expected] of cases) {
    assert.equal(placeboDraftError(draft(patch), mode), expected, JSON.stringify({ patch: Object.keys(patch), mode }));
  }
});

test("a comparison's runs, real arm included, are comparison runs; only its other arms are control arms", () => {
  assert.equal(isComparisonRun({ placebo_comparison_id: null }), false);
  assert.equal(isComparisonRun({ placebo_comparison_id: undefined }), false);
  assert.equal(isComparisonRun({ placebo_comparison_id: "c-1" }), true);
  assert.equal(isControlArm({ arm: undefined }), false);
  assert.equal(isControlArm({ arm: "real" }), false);
  assert.equal(isControlArm({ arm: "placebo" }), true);
  assert.equal(isControlArm({ arm: "no_target" }), true);
});

test("effects read with their sign, interval and unit", () => {
  assert.equal(formatDifference({ estimate: 12.34, ci_low: 8.06, ci_high: 16, p: 0.001, p_adjusted: 0.002 }), "+12.3 [+8.1, +16.0]");
  assert.equal(formatDifference({ estimate: -2, ci_low: -5.5, ci_high: 0, p: 0.4, p_adjusted: null }), "-2.0 [-5.5, 0.0]");
  assert.equal(formatRatio({ estimate: 0.8234, ci_low: 0.75, ci_high: 0.9, p: 0.01, p_adjusted: 0.01 }), "0.82× [0.75, 0.90]");
  assert.equal(formatDeviation(0.032), "+3.2 %");
  assert.equal(formatDeviation(-0.05), "-5.0 %");
  assert.equal(formatDeviation(null), "-");
  assert.equal(formatShare(0.666), "67 %");
  assert.equal(formatShare(null), "-");
});

test("a target key reads back as its target, and a corpus version loses its prefix", () => {
  assert.deepEqual(targetFromKey("persona"), { kind: "persona", skill_id: null });
  assert.deepEqual(targetFromKey("skill:s-1"), { kind: "skill", skill_id: "s-1" });
  assert.equal(shortCorpusVersion("sha256:0123456789abcdef0123"), "0123456789ab");
  assert.equal(shortCorpusVersion("v2"), "v2");
  assert.equal(shortCorpusVersion("sha256:0123456789abcdef,sha256:fedcba9876543210"), "0123456789ab, fedcba987654");
});

test("a warning this UI does not know yet shows its code", () => {
  assert.deepEqual(placeboWarningLabel("few_entries"), { key: "Few entries: read with care", known: true });
  assert.deepEqual(placeboWarningLabel("brand_new_code"), { key: "brand_new_code", known: false });
});
