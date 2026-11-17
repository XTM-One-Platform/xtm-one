/**
 * The agents the map's Schedule view shows - `yarn test:unit` (vitest).
 */
import { describe, expect, it } from "vitest";

import { scheduleScope } from "./schedule-scope";

const set = (...ids: string[]) => new Set(ids);

describe("scheduleScope", () => {
  it("shows every agent when nothing narrows the map", () => {
    expect(scheduleScope({ keptAgentIds: null, focusedIds: set() })).toBeNull();
  });

  it("shows the agents of the segment and the filters", () => {
    expect(scheduleScope({ keptAgentIds: set("a", "b"), focusedIds: set() })).toEqual(set("a", "b"));
  });

  it("narrows to the focused agents", () => {
    expect(scheduleScope({ keptAgentIds: null, focusedIds: set("c") })).toEqual(set("c"));
  });

  it("keeps a focused agent only while the segment keeps it", () => {
    expect(scheduleScope({ keptAgentIds: set("a", "b"), focusedIds: set("b", "z") })).toEqual(set("b"));
    expect(scheduleScope({ keptAgentIds: set("a"), focusedIds: set("z") })).toEqual(set());
  });
});
