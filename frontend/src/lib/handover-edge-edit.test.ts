import { describe, expect, it } from "vitest";
import {
  canEditHandoversOf,
  handoverWriteRefusal,
  planHandoverEdgeAdd,
  planHandoverEdgeEdit,
} from "./handover-edge-edit";
import { ALL_HANDOVER_TYPES } from "./handover-types";
import type { Agent } from "@/types";

const typed = { handover_agent_ids: ["b", "c"], handover_types: { b: "delegate", c: "consult" } };
const legacy = { handover_agent_ids: ["b", "c"], handover_types: {} };

describe("planHandoverEdgeAdd", () => {
  it("refuses the exact handover that already exists", () => {
    expect(planHandoverEdgeAdd(typed, "b", "delegate")).toBe("duplicate");
    expect(planHandoverEdgeAdd(legacy, "b", ALL_HANDOVER_TYPES)).toBe("duplicate");
  });

  it("adds a new target with its type and leaves the others as they are", () => {
    expect(planHandoverEdgeAdd(typed, "d", "transfer")).toEqual({
      ids: ["b", "c", "d"],
      types: { b: "delegate", c: "consult", d: "transfer" },
      narrowed: [],
    });
  });

  it("retypes a target that exists with another type", () => {
    const plan = planHandoverEdgeAdd(typed, "b", "consult");
    expect(plan).not.toBe("duplicate");
    if (plan === "duplicate") return;
    expect(plan.ids).toEqual(["b", "c"]);
    expect(plan.types.b).toBe("consult");
  });

  it("names the targets a first type narrows to delegate in the legacy mode", () => {
    const plan = planHandoverEdgeAdd(legacy, "d", "consult");
    expect(plan).not.toBe("duplicate");
    if (plan === "duplicate") return;
    expect(plan.types.d).toBe("consult");
    expect(plan.narrowed).toEqual(["b", "c"]);
  });

  it("reads an agent with no handover at all", () => {
    expect(planHandoverEdgeAdd({ handover_agent_ids: undefined, handover_types: undefined } as never, "b", "delegate")).toEqual({
      ids: ["b"],
      types: { b: "delegate" },
      narrowed: [],
    });
  });
});

describe("planHandoverEdgeEdit", () => {
  it("removes a target and keeps the others typed", () => {
    expect(planHandoverEdgeEdit(typed, "b", "delete")).toEqual({ ids: ["c"], types: { c: "consult" }, narrowed: [] });
  });

  it("retypes a target", () => {
    expect(planHandoverEdgeEdit(typed, "c", "transfer").types).toEqual({ b: "delegate", c: "transfer" });
  });
});

describe("canEditHandoversOf", () => {
  const owned = { id: "a", user_id: "u1", is_builtin: false } as unknown as Agent;
  const builtin = { id: "g", user_id: "u1", is_builtin: true } as unknown as Agent;
  const other = { id: "o", user_id: "u2", is_builtin: false } as unknown as Agent;

  it("lets the owner edit and keeps another user's agent read only", () => {
    expect(canEditHandoversOf(owned, "u1", false, false)).toBe(true);
    expect(canEditHandoversOf(other, "u1", false, false)).toBe(false);
  });

  it("keeps a built-in agent read only in XTM One mode, even for an admin", () => {
    expect(canEditHandoversOf(builtin, "u1", true, true)).toBe(false);
  });
});

describe("handoverWriteRefusal", () => {
  it("returns the fleet boundary's reason from a 422", () => {
    const error = Object.assign(new Error("'Desk' is a member of the fleet 'Ops' and is not exposed to other fleets."), {
      status: 422,
    });
    expect(handoverWriteRefusal(error)).toBe("'Desk' is a member of the fleet 'Ops' and is not exposed to other fleets.");
  });

  it("returns the reason of a 403 or a 409", () => {
    expect(handoverWriteRefusal(Object.assign(new Error("Not allowed"), { status: 403 }))).toBe("Not allowed");
    expect(handoverWriteRefusal(Object.assign(new Error("Conflict"), { status: 409 }))).toBe("Conflict");
  });

  it("shows no reason for a server failure, a network error or nothing", () => {
    expect(handoverWriteRefusal(Object.assign(new Error("HTTP 502"), { status: 502 }))).toBeUndefined();
    expect(handoverWriteRefusal(new Error("Failed to fetch"))).toBeUndefined();
    expect(handoverWriteRefusal(null)).toBeUndefined();
    expect(handoverWriteRefusal(Object.assign(new Error(" "), { status: 422 }))).toBeUndefined();
  });
});
