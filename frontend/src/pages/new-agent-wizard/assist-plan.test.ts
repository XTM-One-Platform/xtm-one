/**
 * Unit tests for the wizard assistant's plan (AGENT_WIZARD_ASSIST) -
 * `yarn test:unit`.
 *
 * The server answers a patch; the wizard plays it as operations in its own
 * step order, each step opened before what it holds changes, so the person
 * watches the draft filled where they would have filled it. Played in order,
 * the operations leave exactly `draft + patch`; a field the patch repeats or
 * leaves out produces nothing.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  type AssistOp, applyAssistOp, freshDraft, planAssist, toAssistDraft, typingFrames,
} from "./assist-plan.ts";
import type { WizardDraft } from "./draft-storage.ts";

const blank = (): WizardDraft => freshDraft(null, "Bot");
const playAll = (draft: WizardDraft, ops: AssistOp[]) => ops.reduce(applyAssistOp, draft);
const steps = (ops: AssistOp[]) => ops.filter((op) => op.kind === "step").map((op) => op.step);

test("a patch plays in the wizard's step order, each step opened once before its changes", () => {
  const ops = planAssist(blank(), {
    // Given out of order on purpose: the plan follows the wizard, not the patch.
    budget: { max_cost_usd: 1.5 },
    tool_ids: ["builtin:web_fetch"],
    persona: "You triage mail.",
    name: "Mail triage",
    icon: "Mail",
    focus_step: "review",
  });
  assert.deepEqual(steps(ops), ["identity", "persona", "tools", "budget", "review"]);
  assert.deepEqual(
    ops.map((op) => (op.kind === "step" ? `> ${op.step}` : `${op.kind}:${op.target}`)),
    ["> identity", "text:name", "icon:icon", "> persona", "text:persona", "> tools", "toggle:tools", "> budget", "budget:budget", "> review"],
  );
});

test("played in order, the operations leave exactly the draft plus the patch", () => {
  const current: WizardDraft = {
    ...blank(),
    name: "Old",
    toolIds: ["builtin:browser", "builtin:web_fetch"],
    kbIds: ["kb-1"],
    budget: { cost: "5", tokens: "", calls: "", seconds: "" },
  };
  const result = playAll(
    current,
    planAssist(current, {
      name: "New",
      tool_ids: ["builtin:web_fetch", "custom-1"],
      integration_ids: ["int-1"],
      model: "prov::gpt-x",
      memory: { persistent: false, user: null },
      guardrails: "Never delete data.",
      skill_ids: ["sk-1"],
      handover_agent_ids: ["ag-1"],
      budget: { max_cost_usd: null, max_tool_calls: 20 },
    }),
  );
  assert.deepEqual(result, {
    ...current,
    name: "New",
    toolIds: ["builtin:web_fetch", "custom-1"],
    integrationIds: ["int-1"],
    modelChoice: "prov::gpt-x",
    memory: { short: true, medium: true, persistent: false, user: true },
    guardrails: "Never delete data.",
    skillIds: ["sk-1"],
    handoverIds: ["ag-1"],
    budget: { cost: "", tokens: "", calls: "20", seconds: "" },
  });
});

test("a list is the whole new selection: what leaves is unticked before what joins is ticked", () => {
  const current = { ...blank(), kbIds: ["kb-1", "kb-2"] };
  const ops = planAssist(current, { knowledge_base_ids: ["kb-2", "kb-3", "kb-3"] });
  assert.deepEqual(
    ops.flatMap((op) => (op.kind === "toggle" ? [`${op.on ? "+" : "-"}${op.id}`] : [])),
    ["-kb-1", "+kb-3"],
  );
});

test("values the patch repeats or leaves out play nothing", () => {
  const current = { ...blank(), name: "Same", toolIds: ["builtin:web_fetch"] };
  assert.deepEqual(
    planAssist(current, {
      name: "Same",
      tool_ids: ["builtin:web_fetch"],
      memory: { short: true },
      budget: { max_cost_usd: null },
    }),
    [],
  );
  // Without a focus step, the wizard stays where the last change was.
  assert.deepEqual(steps(planAssist(current, { guardrails: "x" })), ["guardrails"]);
});

test("the platform default model is a change like any other", () => {
  const current = { ...blank(), modelChoice: "prov::gpt-x" };
  const ops = planAssist(current, { model: "" });
  assert.equal(playAll(current, ops).modelChoice, "");
});

test("a template's starting point is the one picking it in the gallery leaves", () => {
  const draft = freshDraft(
    { slug: "triage", name: "Triage", icon: "Mail", description: null, default_persona: "You triage." },
    "Bot",
  );
  assert.equal(draft.templateSlug, "triage");
  assert.equal(draft.description, "");
  assert.equal(draft.persona, "You triage.");
  assert.deepEqual(draft.memory, { short: true, medium: true, persistent: true, user: true });
  assert.deepEqual(draft.skillIds, []);
});

test("a template's default skills start selected, as a copy the draft owns", () => {
  const defaults = ["s1", "s2"];
  const draft = freshDraft(
    { slug: "cti", name: "CTI", icon: "Radar", description: null, default_persona: null, default_skill_ids: defaults },
    "Bot",
  );
  assert.deepEqual(draft.skillIds, ["s1", "s2"]);
  draft.skillIds.pop();
  assert.deepEqual(defaults, ["s1", "s2"]);
});

test("typing keeps what both texts share and ends on the new text in bounded frames", () => {
  assert.deepEqual(typingFrames("abc", "abc"), []);
  assert.deepEqual(typingFrames("ab", "abcd", 2), ["abc", "abcd"]);
  const long = "x".repeat(10_000);
  const frames = typingFrames("", long, 40);
  assert.equal(frames.length, 40);
  assert.equal(frames[frames.length - 1], long);
  // A shorter or diverging text replaces the old one from the first difference.
  assert.deepEqual(typingFrames("Hello world", "Help", 10), ["Help"]);
  assert.deepEqual(typingFrames("Hello", "Hi", 10), ["Hi"]);
});

test("the draft the route reads names each field as the route does", () => {
  const draft = { ...blank(), kbIds: ["kb-1"], handoverIds: ["ag-1"], modelChoice: "p::m" };
  assert.deepEqual(toAssistDraft(draft, "knowledge"), {
    template_slug: null,
    step: "knowledge",
    name: "",
    icon: "Bot",
    description: "",
    persona: "",
    guardrails: "",
    tool_ids: [],
    integration_ids: [],
    knowledge_base_ids: ["kb-1"],
    skill_ids: [],
    handover_agent_ids: ["ag-1"],
    model: "p::m",
    memory: { short: true, medium: true, persistent: true, user: true },
    budget: { cost: "", tokens: "", calls: "", seconds: "" },
  });
});
