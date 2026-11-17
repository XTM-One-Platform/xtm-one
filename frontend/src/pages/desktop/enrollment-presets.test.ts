/**
 * Unit tests for what an enrollment key gives the machine it enrolls
 * - `yarn test:unit`.
 *
 * The Desktop preset used to turn shell approval on by itself, with no control
 * on the form: every shell command the agent sent waited for a click nobody had
 * asked for. Approval is now a switch the person turns on, on the Desktop preset
 * as on Custom, and it starts off.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  LOCKED_DOWN_CAPS,
  offersApprovalSwitch,
  resolveEnrollmentPermissions,
  type EnrollmentControls,
} from "./enrollment-presets.ts";
import type { RunnerCapability } from "@/types";

const EVERY_CAP: RunnerCapability[] = ["screenshot", "computer_action", "run_shell", "file_ops", "browser_control"];

function controls(overrides: Partial<EnrollmentControls> = {}): EnrollmentControls {
  return {
    availableCaps: EVERY_CAP,
    mode: "restricted",
    selectedCaps: LOCKED_DOWN_CAPS,
    requireApproval: false,
    ...overrides,
  };
}

test("the Desktop preset gives every capability and holds nothing unless the switch is on", () => {
  assert.deepEqual(resolveEnrollmentPermissions("desktop", controls()), {
    mode: "restricted",
    caps: EVERY_CAP,
    approval: false,
  });
  assert.equal(resolveEnrollmentPermissions("desktop", controls({ requireApproval: true })).approval, true);
});

test("the approval switch is offered wherever a key can hold shell commands", () => {
  assert.equal(offersApprovalSwitch("desktop", "restricted"), true);
  assert.equal(offersApprovalSwitch("custom", "restricted"), true);
  assert.equal(offersApprovalSwitch("custom", "full_control"), false);
  assert.equal(offersApprovalSwitch("locked", "restricted"), false);
});

test("Locked down keeps screenshots and browser control, with no approval to give", () => {
  assert.deepEqual(resolveEnrollmentPermissions("locked", controls({ requireApproval: true })), {
    mode: "restricted",
    caps: LOCKED_DOWN_CAPS,
    approval: false,
  });
});

test("Custom is the controls as set, and full control carries no capability list and no approval", () => {
  assert.deepEqual(
    resolveEnrollmentPermissions("custom", controls({ selectedCaps: ["run_shell"], requireApproval: true })),
    { mode: "restricted", caps: ["run_shell"], approval: true },
  );
  assert.deepEqual(
    resolveEnrollmentPermissions("custom", controls({ mode: "full_control", requireApproval: true })),
    { mode: "full_control", caps: [], approval: false },
  );
});
