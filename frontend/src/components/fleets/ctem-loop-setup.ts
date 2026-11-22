/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The guided CTEM loop setup (#1087, slice 3): the step order, their copy and
 * the pure rules the dialog follows. The states come from `GET
 * /fleets/setup/ctem-loop`; each step is recorded on its own through `PUT
 * /organization-context/setup-prompt/steps`, so leaving midway loses nothing.
 */
import type { CtemSignalCreate } from "@/hooks/api/useCtemSignalsApi";
import type { FleetSetupStatus, FleetSetupStep } from "@/hooks/api/useFleetApi";

export type SetupStepKey = FleetSetupStep["key"];

/** The steps in the order the flow walks them (`organization_setup_prompt.STEPS`). */
export const SETUP_STEPS: readonly SetupStepKey[] = ["organization", "modules", "ecosystem", "gates", "preview", "enable"];

/** @i18n-keys */
export const SETUP_STEP_TITLES = {
  organization: "Organization context",
  modules: "Modules",
  ecosystem: "Security stack",
  gates: "Gates and autonomy",
  preview: "What the fleet will do",
  enable: "Turn on the CTEM loop",
} as const satisfies Record<SetupStepKey, string>;

/** @i18n-keys */
export const SETUP_STEP_INTROS = {
  organization: "The loop ranks every signal against your crown jewels and tech stack, and opens tickets in your ticketing project.",
  modules: "The loop works with the modules connected to this platform. It runs with any of them and says what it cannot do without the others.",
  ecosystem: "Bind the tools the loop's agents act through. A slot left empty only limits what they can do, nothing breaks.",
  gates: "Choose how much the fleet does without asking a person. The line under the levels says what waits at the level picked; you can change it later on the fleet page.",
  preview: "Before anything runs: what the CTEM loop fleet switches on and how many runs a month to expect.",
  enable: "One switch turns on the CTEM loop fleet's automations. They run as you. Nothing ran before this step.",
} as const satisfies Record<SetupStepKey, string>;

/** @i18n-keys */
export const MODULE_DEGRADED = {
  opencti: "Without OpenCTI: no threat or vulnerability signals from intelligence; cases start only from other sources.",
  openaev: "Without OpenAEV: no validation or re-test; cases close on ticket evidence instead of a passing test.",
  opencrq: "Without OpenCRQ: no risk figures; the scope gate reads crown jewels only and decisions carry no expected loss.",
} as const;

export type LoopModule = keyof typeof MODULE_DEGRADED;
export const LOOP_MODULES: readonly LoopModule[] = ["opencti", "openaev", "opencrq"];
export const MODULE_NAMES: Record<LoopModule, string> = { opencti: "OpenCTI", openaev: "OpenAEV", opencrq: "OpenCRQ" };

/** @i18n-keys */
export const ECOSYSTEM_LABELS = {
  edr_xdr: "EDR / XDR",
  ticketing: "Ticketing",
  scanner: "Scanners",
  notification: "Notifications",
} as const;

export type EcosystemCapability = keyof typeof ECOSYSTEM_LABELS;
export const ECOSYSTEM_CAPABILITIES: readonly EcosystemCapability[] = ["edr_xdr", "ticketing", "scanner", "notification"];

/** A step already settled: recorded done or skipped for the current setup version. */
export function isSettled(state: FleetSetupStep["state"] | undefined): boolean {
  return state === "done" || state === "skipped";
}

/** The step the flow opens on: the server's `resume_on`, else the first step (a full replay from the command center). */
export function openingStep(status: Pick<FleetSetupStatus, "resume_on"> | null | undefined): SetupStepKey {
  return status?.resume_on ?? SETUP_STEPS[0];
}

/** The step after *key*, or null after the last one. */
export function nextStep(key: SetupStepKey): SetupStepKey | null {
  const index = SETUP_STEPS.indexOf(key);
  return index >= 0 && index < SETUP_STEPS.length - 1 ? SETUP_STEPS[index + 1] : null;
}

/** The step before *key*, or null on the first one. */
export function previousStep(key: SetupStepKey): SetupStepKey | null {
  const index = SETUP_STEPS.indexOf(key);
  return index > 0 ? SETUP_STEPS[index - 1] : null;
}

/** One step of the status by key, or undefined while it is not read. */
export function stepOf(status: FleetSetupStatus | null | undefined, key: SetupStepKey): FleetSetupStep | undefined {
  return status?.steps.find((step) => step.key === key);
}

/** The connected loop modules a Modules step's facts name, in the loop's order. */
export function connectedModules(step: FleetSetupStep | undefined): LoopModule[] {
  const connected = new Set(Array.isArray(step?.facts.connected) ? (step.facts.connected as string[]) : []);
  return LOOP_MODULES.filter((module) => connected.has(module));
}

/** Each ecosystem capability's best slot state as the Ecosystem step's facts give it; unread reads `unavailable`. */
export function ecosystemSlots(step: FleetSetupStep | undefined): Record<EcosystemCapability, "filled" | "empty" | "unavailable"> {
  const raw = (step?.facts.slots ?? {}) as Record<string, string>;
  const pick = (value: string | undefined) => (value === "filled" || value === "empty" ? value : "unavailable");
  return {
    edr_xdr: pick(raw.edr_xdr),
    ticketing: pick(raw.ticketing),
    scanner: pick(raw.scanner),
    notification: pick(raw.notification),
  };
}

/**
 * The test signal "Send a test signal" records at the end of the setup: a new
 * vulnerability seen through the world lens, marked as a test in its evidence,
 * with a dedupe key of its own so every send is a new row the loop processes.
 * It goes through the real intake, reconcile and scope gate: no fake state.
 */
export function testSignal(now: Date): CtemSignalCreate {
  const stamp = now.toISOString();
  return {
    trigger_path: "new_vulnerability",
    lens: "world",
    source: { provider: "manual", external_id: `setup-test-${stamp}` },
    subject: { kind: "vulnerability", ref: "CVE-0000-0001", label: "Test signal from the CTEM loop setup" },
    observed_at: stamp,
    evidence: { test: true, origin: "ctem-loop-setup" },
    dedupe_key: `ctem-loop-setup-test:${stamp}`,
  };
}

/** The command center with the loop trace of one signal open (`?loop=signal:<id>`). */
export function signalTracePath(signalId: string): string {
  return `/?loop=${encodeURIComponent(`signal:${signalId}`)}`;
}

/**
 * Whether the CTEM loop runs: at least one automation of the fleet is on, as
 * the enable step reads it (`automations_enabled`). The end of the setup says
 * "set up" and offers a test signal only then (UX r2 B1: an admin who skipped
 * or finished without "Review and enable" has a saved setup and a loop that is off).
 */
export function loopRuns(status: FleetSetupStatus | null | undefined): boolean {
  return countFact(stepOf(status, "enable"), "automations_enabled") > 0;
}

/** A number fact, 0 when absent or not a number. */
export function countFact(step: FleetSetupStep | undefined, name: string): number {
  const value = step?.facts[name];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
