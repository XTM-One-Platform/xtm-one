// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ctemMayDecide, mayAnswerCaseGate } from "./ctem-access";

describe("ctemMayDecide", () => {
  it("lets an administrator and a decide member act", () => {
    expect(ctemMayDecide(null, true)).toBe(true);
    expect(ctemMayDecide({ ctem_access: "decide" }, false)).toBe(true);
    expect(ctemMayDecide({ ctem_access: "admin" }, false)).toBe(true);
  });

  it("keeps a view member and a person without a right read only", () => {
    expect(ctemMayDecide({ ctem_access: "view" }, false)).toBe(false);
    expect(ctemMayDecide({ ctem_access: null }, false)).toBe(false);
    expect(ctemMayDecide(undefined, false)).toBe(false);
  });
});

describe("mayAnswerCaseGate", () => {
  it("answers a private gate whatever the right", () => {
    expect(mayAnswerCaseGate({ is_company_managed: false }, { ctem_access: null }, false)).toBe(true);
  });

  it("answers a company gate only as an administrator or a decide member", () => {
    const item = { is_company_managed: true };
    expect(mayAnswerCaseGate(item, { ctem_access: "decide" }, false)).toBe(true);
    expect(mayAnswerCaseGate(item, null, true)).toBe(true);
    expect(mayAnswerCaseGate(item, { ctem_access: "view" }, false)).toBe(false);
    expect(mayAnswerCaseGate(item, { ctem_access: null }, false)).toBe(false);
  });
});
