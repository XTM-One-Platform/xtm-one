// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ctemMayDecide, ctemMayView, homeIsCtemCommandCenter, mayAnswerCaseGate } from "./ctem-access";

describe("ctemMayView", () => {
  it("lets an administrator and a view or decide member reach CTEM (#2441)", () => {
    expect(ctemMayView(null, true)).toBe(true);
    expect(ctemMayView({ ctem_access: "view" }, false)).toBe(true);
    expect(ctemMayView({ ctem_access: "decide" }, false)).toBe(true);
    expect(ctemMayView({ ctem_access: "admin" }, false)).toBe(true);
  });

  it("hides CTEM from a person without a right", () => {
    expect(ctemMayView({ ctem_access: null }, false)).toBe(false);
    expect(ctemMayView(undefined, false)).toBe(false);
  });
});

describe("homeIsCtemCommandCenter", () => {
  it("shows the command center in XTM One mode to an administrator and a view or decide member", () => {
    expect(homeIsCtemCommandCenter(true, null, true)).toBe(true);
    expect(homeIsCtemCommandCenter(true, { ctem_access: "view" }, false)).toBe(true);
    expect(homeIsCtemCommandCenter(true, { ctem_access: "decide" }, false)).toBe(true);
  });

  it("gives a person without a CTEM right the mode-neutral home (#2441)", () => {
    expect(homeIsCtemCommandCenter(true, { ctem_access: null }, false)).toBe(false);
    expect(homeIsCtemCommandCenter(true, undefined, false)).toBe(false);
  });

  it("never shows it outside XTM One mode", () => {
    expect(homeIsCtemCommandCenter(false, null, true)).toBe(false);
    expect(homeIsCtemCommandCenter(false, { ctem_access: "decide" }, false)).toBe(false);
  });
});

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
