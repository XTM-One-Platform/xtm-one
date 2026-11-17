// The "Duplicate skill" dialog's pure parts (#9174): the request it sends,
// what a refusal says, the toast after a fork and the provenance line.
import { test } from "vitest";
import assert from "node:assert/strict";

import type { SkillForkResult } from "@/hooks/api/useSkillApi";
import type { AgentSkill } from "@/types";

import {
  defaultForkSkillName,
  describeSkillForkRefusal,
  initialForkSkillForm,
  skillForkProvenance,
  skillForkSummary,
  toForkSkillRequest,
} from "./fork-skill-form";

const t = (text: string, values?: Record<string, string | number>) =>
  values ? `${text} ${JSON.stringify(values)}` : text;

test("the form starts on the localized Copy of the source's name, no fleet re-bound", () => {
  assert.deepEqual(initialForkSkillForm({ name: "Vulnerability triage" }, t), {
    name: 'Copy of {name} {"name":"Vulnerability triage"}',
    repointFleetId: null,
  });
  // The default goes through the translator: a French reader never sees "Copy of".
  const fr = (text: string, values?: Record<string, string | number>) =>
    text === "Copy of {name}" ? `Copie de ${values?.name}` : text;
  assert.equal(defaultForkSkillName({ name: "FIMI Threat Brief" }, fr), "Copie de FIMI Threat Brief");
  assert.equal(initialForkSkillForm({ name: "FIMI Threat Brief" }, fr).name, "Copie de FIMI Threat Brief");
});

test("the request trims the name and sends the localized default for an empty one", () => {
  assert.deepEqual(toForkSkillRequest({ name: "  My triage  ", repointFleetId: "f1" }, "Copie de X"), {
    name: "My triage",
    repoint_fleet_id: "f1",
  });
  assert.equal(toForkSkillRequest({ name: "   ", repointFleetId: null }, "Copie de X").name, "Copie de X");
});

test("a refusal says why, with the API's words as a detail", () => {
  const forbidden = Object.assign(new Error("Not allowed"), { status: 403 });
  assert.deepEqual(describeSkillForkRefusal(forbidden, (s) => s), {
    message: "You are not allowed to duplicate this skill.",
    detail: "Not allowed",
  });
  const empty = Object.assign(new Error("no content"), { status: 400 });
  assert.match(describeSkillForkRefusal(empty, (s) => s).message, /no content yet/);
  assert.deepEqual(describeSkillForkRefusal("boom", (s) => s), { message: "The skill was not duplicated.", detail: null });
});

function result(extra: Partial<SkillForkResult> = {}): SkillForkResult {
  return {
    skill: { id: "s2", name: "My triage" } as AgentSkill,
    resources_copied: 0,
    repointed_agent_ids: [],
    warnings: [],
    ...extra,
  };
}

test("the toast names the copy, counts the agents re-bound, and lists what stayed on the source", () => {
  const clean = skillForkSummary(result(), t);
  assert.equal(clean.variant, "success");
  assert.equal(clean.lines.length, 1);

  const one = skillForkSummary(result({ repointed_agent_ids: ["a1"] }), t);
  assert.equal(one.lines[1], 'Agents of the fleet now using the copy: {count}. {"count":1}');

  const partial = skillForkSummary(result({ warnings: ["1 member agent(s) you cannot edit still use the source skill"] }), t);
  assert.equal(partial.variant, "info");
  assert.equal(partial.lines[partial.lines.length - 1], "1 member agent(s) you cannot edit still use the source skill");
});

test("only a fork carries a provenance line", () => {
  assert.equal(skillForkProvenance({ forked_from: null }), null);
  assert.equal(skillForkProvenance({}), null);
  assert.deepEqual(
    skillForkProvenance({
      forked_from: {
        skill_id: "s1",
        name: "Vulnerability triage",
        slug: "vuln-triage",
        is_builtin: true,
        version_number: 3,
        forked_at: "2026-11-06T20:00:00Z",
      },
    }),
    { name: "Vulnerability triage", isBuiltin: true, sourceId: "s1", version: 3 },
  );
});
