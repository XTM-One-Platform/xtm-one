/**
 * Unit tests for the handover type map of the General Assistant and of an
 * agent - `yarn test:unit`.
 *
 * An empty map gives every handover tool for every target. A save of a
 * settings screen, or a target added to it, must not narrow such a
 * configuration to delegate; only picking a type makes the map explicit, and
 * the targets that picking narrows are named before it happens. The shared
 * cases are the ONE table the backend rules (`app/services/handover_types.py`)
 * are checked against too.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  ALL_HANDOVER_TYPES,
  consultsBackgroundDelegator,
  explicitHandoverTypes,
  isLegacyHandoverMap,
  runnableHandoverTypes,
  runsEveryHandoverType,
  shownHandoverType,
  withAddedHandoverTarget,
  withHandoverEdge,
  withHandoverType,
  withoutHandoverEdge,
  withRemovedHandoverTarget,
} from "./handover-types.ts";

const RUNNABLE = ["delegate", "consult", "transfer"] as const;

test("a consult into an agent that delegates in the background is named, nothing else", () => {
  const delegator = { handover_agent_ids: ["x", "y"], handover_types: { x: "consult", y: "delegate" } };
  assert.equal(consultsBackgroundDelegator("consult", delegator), true);
  // A target without a type delegates, and so does one in the legacy mode.
  assert.equal(consultsBackgroundDelegator("consult", { handover_agent_ids: ["x"], handover_types: { y: "consult" } }), true);
  assert.equal(consultsBackgroundDelegator("consult", { handover_agent_ids: ["x"], handover_types: null }), true);
  assert.equal(consultsBackgroundDelegator("delegate", delegator), false);
  assert.equal(consultsBackgroundDelegator("transfer", delegator), false);
  assert.equal(consultsBackgroundDelegator("consult", { handover_agent_ids: ["x"], handover_types: { x: "consult" } }), false);
  assert.equal(consultsBackgroundDelegator("consult", { handover_agent_ids: [], handover_types: {} }), false);
  assert.equal(consultsBackgroundDelegator("consult", undefined), false);
});

test("an empty map stays empty when the screen is edited", () => {
  assert.deepEqual(runnableHandoverTypes(["a", "b"], {}, RUNNABLE), {});
  assert.deepEqual(runnableHandoverTypes(["a"], null, RUNNABLE), {});
  assert.deepEqual(runnableHandoverTypes(["a"], undefined, RUNNABLE), {});
});

test("an explicit map is written out for every target, as each one runs", () => {
  assert.deepEqual(
    runnableHandoverTypes(["a", "b", "c"], { a: "consult", c: "mandatory_transfer", gone: "transfer" }, RUNNABLE),
    { a: "consult", b: "delegate", c: "delegate" },
  );
});

test("a target added next to targets without a type keeps the map empty", () => {
  assert.deepEqual(withAddedHandoverTarget(["a"], {}, "new"), {});
});

test("a first target, like one added to an explicit map, is recorded as delegate", () => {
  assert.deepEqual(withAddedHandoverTarget([], {}, "new"), { new: "delegate" });
  assert.deepEqual(withAddedHandoverTarget(["a"], { a: "consult" }, "new"), { a: "consult", new: "delegate" });
});

test("a target of an empty map is shown as running every handover type, never as delegate", () => {
  for (const types of [{}, null, undefined]) {
    assert.equal(isLegacyHandoverMap(types), true);
    assert.equal(shownHandoverType(types, "a", RUNNABLE), ALL_HANDOVER_TYPES);
  }
});

test("a target of an explicit map is shown as it runs", () => {
  const types = { a: "consult", b: "mandatory_transfer" };
  assert.equal(isLegacyHandoverMap(types), false);
  assert.equal(shownHandoverType(types, "a", RUNNABLE), "consult");
  assert.equal(shownHandoverType(types, "b", RUNNABLE), "delegate");
  assert.equal(shownHandoverType(types, "untyped", RUNNABLE), "delegate");
});

test("a stored all runs every type, an entry of several types is shown by the first it runs (#2207)", () => {
  const types = { a: "all", b: "delegate|consult", c: "transfer" };
  assert.equal(shownHandoverType(types, "a", RUNNABLE), ALL_HANDOVER_TYPES);
  assert.equal(shownHandoverType(types, "b", RUNNABLE), "consult");
  assert.equal(shownHandoverType(types, "c", RUNNABLE), "transfer");
});

test("the first type picked in an empty map types every target and names the ones it narrows", () => {
  assert.deepEqual(withHandoverType(["a", "b", "c"], {}, "b", "consult"), {
    types: { a: "delegate", b: "consult", c: "delegate" },
    narrowed: ["a", "c"],
  });
});

test("picking delegate first still narrows the other targets, and says so", () => {
  assert.deepEqual(withHandoverType(["a", "b"], {}, "a", "delegate"), {
    types: { a: "delegate", b: "delegate" },
    narrowed: ["b"],
  });
});

test("the only target of an empty map narrows no other one", () => {
  assert.deepEqual(withHandoverType(["a"], {}, "a", "transfer"), { types: { a: "transfer" }, narrowed: [] });
});

test("picking a type in an explicit map changes that target alone", () => {
  assert.deepEqual(withHandoverType(["a", "b"], { a: "consult", b: "delegate" }, "b", "transfer"), {
    types: { a: "consult", b: "transfer" },
    narrowed: [],
  });
});

test("removing a target drops its type and keeps the others", () => {
  assert.deepEqual(withRemovedHandoverTarget({ a: "consult", b: "delegate" }, "a"), { b: "delegate" });
  assert.deepEqual(withRemovedHandoverTarget({}, "a"), {});
});

test("no edit of the screen changes how a target runs unless it is the one picked or it is named first", () => {
  type Draft = { ids: string[]; types: Record<string, string> };
  type Step = { draft: Draft; picked?: string; narrowed: string[] };
  const pool = ["a", "b", "c"];
  const runs = (draft: Draft, id: string) => shownHandoverType(draft.types, id, RUNNABLE);

  // Everything the screen lets an administrator do to the handover draft.
  const steps = (draft: Draft): Step[] => {
    const toggles = pool.map((id): Step => {
      const removing = draft.ids.includes(id);
      const ids = removing ? draft.ids.filter((other) => other !== id) : [...draft.ids, id];
      const types = removing
        ? withRemovedHandoverTarget(draft.types, id)
        : withAddedHandoverTarget(draft.ids, draft.types, id);
      return { draft: { ids, types }, narrowed: [] };
    });
    const picks = draft.ids.flatMap((id) =>
      RUNNABLE.map((type): Step => {
        const change = withHandoverType(draft.ids, draft.types, id, type);
        return { draft: { ids: draft.ids, types: change.types }, picked: id, narrowed: change.narrowed };
      }),
    );
    return [...toggles, ...picks];
  };

  const walk = (draft: Draft, depth: number) => {
    // What is saved is what is shown: an explicit draft types every target and nothing else.
    if (!isLegacyHandoverMap(draft.types)) {
      assert.deepEqual(Object.keys(draft.types).sort(), [...draft.ids].sort());
    }
    if (depth === 0) return;
    for (const step of steps(draft)) {
      const changed = draft.ids.filter(
        (id) => step.draft.ids.includes(id) && id !== step.picked && runs(step.draft, id) !== runs(draft, id),
      );
      assert.deepEqual(changed, step.narrowed, `unannounced change from ${JSON.stringify(draft)}`);
      walk(step.draft, depth - 1);
    }
  };

  // Every shape the screen opens: legacy, explicit, explicit through a stale key only, and empty.
  const opened: [string[], Record<string, string>][] = [
    [["a", "b"], {}],
    [["a", "b"], { a: "consult" }],
    [["a"], { gone: "transfer" }],
    [[], {}],
  ];
  for (const [ids, stored] of opened) walk({ ids, types: runnableHandoverTypes(ids, stored, RUNNABLE) }, 4);
});

// ── Agents ──────────────────────────────────────────────────────────

const AGENT_TYPES = ["delegate", "consult", "transfer", "mandatory_transfer"] as const;

test("an agent is in the legacy mode only while it has targets and no type", () => {
  assert.equal(runsEveryHandoverType(["a"], {}), true);
  assert.equal(runsEveryHandoverType(["a"], null), true);
  assert.equal(runsEveryHandoverType([], {}), false);
  assert.equal(runsEveryHandoverType(["a"], { a: "consult" }), false);
});

test("an agent's explicit map is written out with every stored type kept", () => {
  assert.deepEqual(
    explicitHandoverTypes(["a", "b", "c", "d"], { a: "consult", b: "mandatory_transfer", c: "custom", gone: "transfer" }),
    { a: "consult", b: "mandatory_transfer", c: "custom", d: "delegate" },
  );
});

test("an edge drawn as every type keeps a legacy agent in the legacy mode", () => {
  assert.deepEqual(withHandoverEdge(["a", "b"], {}, "c", ALL_HANDOVER_TYPES), {
    ids: ["a", "b", "c"],
    types: {},
    narrowed: [],
  });
});

test("an edge given a type in the legacy mode types every target and names the ones it narrows", () => {
  assert.deepEqual(withHandoverEdge(["a", "b"], {}, "c", "consult"), {
    ids: ["a", "b", "c"],
    types: { a: "delegate", b: "delegate", c: "consult" },
    narrowed: ["a", "b"],
  });
  assert.deepEqual(withHandoverEdge(["a", "b"], {}, "b", "transfer"), {
    ids: ["a", "b"],
    types: { a: "delegate", b: "transfer" },
    narrowed: ["a"],
  });
});

test("the first edge of an agent without targets is typed as picked, delegate by default", () => {
  assert.deepEqual(withHandoverEdge([], {}, "a", "delegate"), { ids: ["a"], types: { a: "delegate" }, narrowed: [] });
  assert.deepEqual(withHandoverEdge([], null, "a", "consult"), { ids: ["a"], types: { a: "consult" }, narrowed: [] });
});

test("an edge of an explicit agent changes that target alone", () => {
  assert.deepEqual(withHandoverEdge(["a", "b"], { a: "consult" }, "b", "transfer"), {
    ids: ["a", "b"],
    types: { a: "consult", b: "transfer" },
    narrowed: [],
  });
  // Every type is not offered outside the legacy mode; it would be saved as delegate.
  assert.deepEqual(withHandoverEdge(["a"], { a: "consult" }, "c", ALL_HANDOVER_TYPES).types, {
    a: "consult",
    c: "delegate",
  });
});

test("removing the only typed target never turns the others into the legacy mode", () => {
  assert.deepEqual(withoutHandoverEdge(["a", "b"], { a: "consult" }, "a"), { ids: ["b"], types: { b: "delegate" } });
  assert.deepEqual(withoutHandoverEdge(["a", "b"], {}, "a"), { ids: ["b"], types: {} });
});

test("no flow map edit changes how a target runs unless it is the one edited or it is named first", () => {
  type Agent = { ids: string[]; types: Record<string, string> };
  type Step = { agent: Agent; edited?: string; narrowed: string[] };
  const pool = ["a", "b", "c"];
  const runs = (agent: Agent, id: string) => shownHandoverType(agent.types, id, AGENT_TYPES);

  // Every edit the flow map lets a user make: draw, retype or delete an edge.
  const steps = (agent: Agent): Step[] =>
    pool.flatMap((id): Step[] => {
      const offered: readonly string[] = runsEveryHandoverType(agent.ids, agent.types)
        ? [ALL_HANDOVER_TYPES, ...AGENT_TYPES]
        : AGENT_TYPES;
      const edits = offered.map((type): Step => {
        const change = withHandoverEdge(agent.ids, agent.types, id, type);
        return { agent: { ids: change.ids, types: change.types }, edited: id, narrowed: change.narrowed };
      });
      if (!agent.ids.includes(id)) return edits;
      return [...edits, { agent: withoutHandoverEdge(agent.ids, agent.types, id), edited: id, narrowed: [] }];
    });

  const walk = (agent: Agent, depth: number) => {
    if (depth === 0) return;
    for (const step of steps(agent)) {
      const changed = agent.ids.filter(
        (id) => step.agent.ids.includes(id) && id !== step.edited && runs(step.agent, id) !== runs(agent, id),
      );
      assert.deepEqual(changed, step.narrowed, `unannounced change from ${JSON.stringify(agent)}`);
      walk(step.agent, depth - 1);
    }
  };

  // Legacy, explicit with an untyped target, explicit through a mandatory transfer, and no target.
  const opened: Agent[] = [
    { ids: ["a", "b"], types: {} },
    { ids: ["a", "b"], types: { a: "consult" } },
    { ids: ["a", "b"], types: { a: "mandatory_transfer" } },
    { ids: [], types: {} },
  ];
  for (const agent of opened) walk(agent, 3);
});

// ── The table the backend rules are checked against too ─────────────────────

type TypeMap = Record<string, string> | null;

interface HandoverTypeCases {
  legacy: { targets: string[]; types: TypeMap; runs_every_type: boolean }[];
  added: { targets: string[]; types: TypeMap; add: string; expected: Record<string, string> }[];
  removed: { targets: string[]; types: TypeMap; remove: string; runs: Record<string, string> }[];
}

const cases = JSON.parse(
  readFileSync(new URL("../../tests_e2e/fixtures/handover-type-cases.json", import.meta.url), "utf8"),
) as HandoverTypeCases;

test("shared cases: targets run every type only while no target has one", () => {
  for (const c of cases.legacy) {
    assert.equal(runsEveryHandoverType(c.targets, c.types), c.runs_every_type, JSON.stringify(c));
  }
});

test("shared cases: an added target joins targets without a type, or is recorded as delegate", () => {
  for (const c of cases.added) {
    assert.deepEqual(withAddedHandoverTarget(c.targets, c.types, c.add), c.expected, JSON.stringify(c));
  }
});

test("shared cases: removing a target never turns an explicit map into the legacy mode", () => {
  for (const c of cases.removed) {
    const { ids, types } = withoutHandoverEdge(c.targets, c.types, c.remove);
    const runs = Object.fromEntries(
      ids.map((id) => {
        const shown = shownHandoverType(types, id, AGENT_TYPES);
        return [id, shown === ALL_HANDOVER_TYPES ? "all" : shown];
      }),
    );
    assert.deepEqual(runs, c.runs, JSON.stringify(c));
  }
});

// -- The writes of main's screens --------------------------------------------
// Copied from Settings > AI and models > General Assistant, the agent configuration and the
// flow map as they are on main: every map that is neither a legacy map with
// targets nor emptied by a removal is written exactly as they wrote it.

type Types = Record<string, string>;

const mainGaEdit = (targetIds: string[], stored: TypeMap): Types => {
  const runnable: Types = {};
  for (const id of targetIds) {
    const type = stored?.[id];
    runnable[id] = type !== undefined && (RUNNABLE as readonly string[]).includes(type) ? type : "delegate";
  }
  return runnable;
};
const mainGaAdd = (types: Types, id: string): Types => ({ ...types, [id]: "delegate" });
const mainGaRemove = (types: Types, id: string): Types => {
  const { [id]: _removed, ...rest } = types;
  return rest;
};
const mainPick = (types: Types, id: string, type: string): Types => ({ ...types, [id]: type });
const mainAgentSave = (ids: string[], types: Types): Types => {
  const clean: Types = {};
  for (const id of ids) clean[id] = types[id] || "delegate";
  return clean;
};
const mainEdgeDraw = (ids: string[], types: Types, id: string, type: string) => ({
  ids: ids.includes(id) ? [...ids] : [...ids, id],
  types: { ...types, [id]: type },
});
const mainEdgeDelete = (ids: string[], types: Types, id: string) => {
  const { [id]: _removed, ...rest } = types;
  return { ids: ids.filter((target) => target !== id), types: rest };
};

const TARGET_LISTS = [[], ["a"], ["a", "b"], ["b", "a", "c"]];
const EXPLICIT_MAPS: Types[] = [
  { a: "consult" },
  { b: "delegate" },
  { a: "transfer", b: "mandatory_transfer" },
  { stale: "consult" },
  { a: "bogus" },
  { a: "" },
];
const POOL = ["a", "b", "c", "d"];

test("the settings screen writes what main wrote for a map that holds a type", () => {
  for (const ids of TARGET_LISTS) {
    for (const stored of EXPLICIT_MAPS) {
      const context = JSON.stringify({ ids, stored });
      const draft = runnableHandoverTypes(ids, stored, RUNNABLE);
      assert.deepEqual(draft, mainGaEdit(ids, stored), context);
      for (const id of POOL.filter((agentId) => !ids.includes(agentId))) {
        assert.deepEqual(withAddedHandoverTarget(ids, draft, id), mainGaAdd(draft, id), context);
      }
      for (const id of ids) {
        assert.deepEqual(withRemovedHandoverTarget(draft, id), mainGaRemove(draft, id), context);
        for (const type of RUNNABLE) {
          assert.deepEqual(withHandoverType(ids, draft, id, type), { types: mainPick(draft, id, type), narrowed: [] }, context);
        }
      }
    }
  }
  // A first target of a configuration without targets, as on main.
  for (const stored of [{}, null]) assert.deepEqual(withAddedHandoverTarget([], stored, "a"), { a: "delegate" });
});

test("the agent configuration saves what main saved for a map that holds a type", () => {
  for (const ids of TARGET_LISTS) {
    for (const types of [...EXPLICIT_MAPS, {}]) {
      assert.deepEqual(explicitHandoverTypes(ids, types), mainAgentSave(ids, types), JSON.stringify({ ids, types }));
    }
  }
});

test("the flow map saves what main saved, except a removal that would empty a typed map", () => {
  for (const ids of TARGET_LISTS) {
    for (const types of [...EXPLICIT_MAPS, ...(ids.length === 0 ? [{}] : [])]) {
      const context = JSON.stringify({ ids, types });
      for (const id of POOL) {
        for (const type of AGENT_TYPES) {
          const { narrowed, ...saved } = withHandoverEdge(ids, types, id, type);
          assert.deepEqual(saved, mainEdgeDraw(ids, types, id, type), context);
          assert.deepEqual(narrowed, [], context);
        }
      }
      for (const id of ids) {
        const main = mainEdgeDelete(ids, types, id);
        const saved = withoutHandoverEdge(ids, types, id);
        const emptied = Object.keys(types).length > 0 && Object.keys(main.types).length === 0 && main.ids.length > 0;
        // The one change: the targets left keep running as delegate, how they ran.
        const expected = emptied ? { ids: main.ids, types: mainAgentSave(main.ids, {}) } : main;
        assert.deepEqual(saved, expected, `${context} remove ${id}`);
      }
    }
  }
});
