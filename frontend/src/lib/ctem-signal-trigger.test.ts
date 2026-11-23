// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import type { TriggerConfig } from "@/types";
import {
  CTEM_SIGNAL_TILE,
  ctemSignalConfig,
  ctemSignalDraft,
  isCtemSignalTrigger,
  triggerTile,
  triggerTiles,
} from "./ctem-signal-trigger";
import { TRIGGER_ICONS, TRIGGER_LABELS } from "./constants";

const cfg = (value: Record<string, unknown>) => value as unknown as TriggerConfig;

describe("CTEM signal trigger (#1409)", () => {
  it("saves both events and no filter by default", () => {
    const draft = ctemSignalDraft("manual", undefined);
    expect(draft.on).toBe("both");
    expect(ctemSignalConfig(draft)).toEqual({ event: ["ctem_signal.recorded", "ctem_signal.triaged"] });
  });

  it("saves one event and an any-of list per filled filter", () => {
    const draft = ctemSignalDraft("manual", undefined);
    draft.on = "triaged";
    draft.filters.verdict = ["confirmed"];
    draft.filters.trigger_path = ["new_exposure", "credential_leak"];
    expect(ctemSignalConfig(draft)).toEqual({
      event: "ctem_signal.triaged",
      filter: { trigger_path: ["new_exposure", "credential_leak"], verdict: ["confirmed"] },
    });
  });

  it("reopens a saved CTEM signal trigger on its tile with its draft", () => {
    const saved = cfg({ event: "ctem_signal.recorded", filter: { lens: ["outside"], verdict: "to_validate" } });
    expect(isCtemSignalTrigger("platform_event", saved)).toBe(true);
    expect(triggerTile("platform_event", saved)).toBe(CTEM_SIGNAL_TILE);
    const draft = ctemSignalDraft("platform_event", saved);
    expect(draft.on).toBe("recorded");
    expect(draft.filters.lens).toEqual(["outside"]);
    expect(draft.filters.verdict).toEqual(["to_validate"]);
    expect(ctemSignalConfig(draft)).toEqual({ event: "ctem_signal.recorded", filter: { lens: ["outside"], verdict: ["to_validate"] } });
  });

  it("keeps the generic tile for a trigger the CTEM tile would change", () => {
    const cases = [
      cfg({ event: ["ctem_signal.recorded", "ctem_case.opened"] }),
      cfg({ event: "ctem_signal.recorded", once: true }),
      cfg({ event: "ctem_signal.recorded", expires_at: "2027-01-01T00:00:00Z" }),
      cfg({ event: "ctem_signal.recorded", scope: "all" }),
      cfg({ event: "ctem_signal.recorded", filter: { case_id: "x" } }),
      cfg({ event: "ctem_signal.recorded", filter: { verdict: { $ne: "discarded" } } }),
      cfg({ event: "run.failed" }),
    ];
    for (const config of cases) expect(triggerTile("platform_event", config)).toBe("platform_event");
    expect(triggerTile("schedule", cfg({ event: "ctem_signal.recorded" }))).toBe("schedule");
  });

  it("names a saved CTEM signal trigger the same way everywhere it is listed (UX round 1, B1)", () => {
    const saved = cfg({ event: ["ctem_signal.recorded", "ctem_signal.triaged"], filter: { verdict: ["confirmed"] } });
    const tile = triggerTile("platform_event", saved);
    expect(TRIGGER_LABELS[tile]).toBe("CTEM signal");
    expect(TRIGGER_ICONS[tile]).toBe(TRIGGER_ICONS[CTEM_SIGNAL_TILE]);
    expect(TRIGGER_ICONS[tile]).not.toBe(TRIGGER_ICONS.platform_event);
    expect(TRIGGER_LABELS[triggerTile("platform_event", cfg({ event: "run.failed" }))]).toBe(TRIGGER_LABELS.platform_event);
  });

  it("places the tile after Platform event on XTM One only", () => {
    const types = ["schedule", "manual", "platform_event", "other"];
    expect(triggerTiles(types, true)).toEqual(["schedule", "manual", "platform_event", CTEM_SIGNAL_TILE, "other"]);
    expect(triggerTiles(types, false)).toEqual(types);
  });
});
