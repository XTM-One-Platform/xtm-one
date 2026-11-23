// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * A CTEM case state, priority, loop severity or signal state is drawn by the
 * shared ToneBadge (components/ui/tone-badge.tsx, #1267: the library Chip's
 * single style since design system 1.13.0, #1595), never by the library Chip
 * directly or by a hand-written tone badge (UX round 1 B1
 * of #1270). Static, like the other design-system guards.
 */

const CTEM = path.resolve(__dirname, "../src/pages/xtm-one/ctem");

/** The label maps and tone names that only a state, priority or severity chip carries. */
const STATUS_CHIP = /CASE_STATE_LABEL|CASE_PRIORITY_LABEL|SIGNAL_STATE_LABEL|(STATE|PRIORITY|SEVERITY)_TONE\b|severity=\{[^}]*\b(priority|severity|caseState|signalState)\b/;
/** A map from a CTEM state, priority or severity to anything but a HotTone. */
const TONE_MAP = /Record<\s*(CtemCaseState|CtemCasePriority|CtemCaseSeverity|CtemSignalState)\s*,\s*([A-Za-z]+)\s*>/g;
const TONE_MAP_NAME = /const\s+\w*(TONE|SEVERITY|BADGE|VARIANT)\w*\s*:\s*(Partial<)?\s*Record</;

async function sources(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = await Promise.all(
    entries.map((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return sources(full);
      return Promise.resolve(/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : []);
    }),
  );
  return files.flat();
}

/** Each `<Chip ... />` or `<Chip ...>` opening tag of a source, whole. */
function chipTags(source: string): string[] {
  return [...source.matchAll(/<Chip\b[\s\S]*?\/?>/g)].map((m) => m[0]);
}

/** Each `<Badge ...>` opening tag of a source, whole. */
function badgeTags(source: string): string[] {
  return [...source.matchAll(/<Badge\b[\s\S]*?>/g)].map((m) => m[0]);
}

describe("CTEM status chips", () => {
  it("never draw a state, priority or severity with the library Chip", async () => {
    const offenders: string[] = [];
    for (const file of await sources(CTEM)) {
      for (const tag of chipTags(await readFile(file, "utf8"))) {
        if (STATUS_CHIP.test(tag)) offenders.push(`${path.relative(CTEM, file)}: ${tag.replace(/\s+/g, " ").slice(0, 120)}`);
      }
    }
    expect(offenders, "use ToneBadge from @/components/ui/tone-badge").toEqual([]);
  });

  it("map a state, priority or severity to a HotTone only", async () => {
    const offenders: string[] = [];
    for (const file of await sources(CTEM)) {
      for (const line of (await readFile(file, "utf8")).split("\n")) {
        if (!TONE_MAP_NAME.test(line)) continue;
        for (const [, key, value] of line.matchAll(TONE_MAP)) {
          if (value !== "HotTone") offenders.push(`${path.relative(CTEM, file)}: ${key} -> ${value}`);
        }
      }
    }
    expect(offenders, "type the tone map Record<..., HotTone> and render ToneBadge").toEqual([]);
  });

  it("never hand-write a tone badge from the feedback tokens", async () => {
    const offenders: string[] = [];
    for (const file of await sources(CTEM)) {
      for (const tag of badgeTags(await readFile(file, "utf8"))) {
        if (/feedback-[a-z]+-(primary|secondary|tertiary)/.test(tag)) offenders.push(`${path.relative(CTEM, file)}: ${tag.replace(/\s+/g, " ").slice(0, 120)}`);
      }
    }
    expect(offenders, "use ToneBadge from @/components/ui/tone-badge").toEqual([]);
  });

  it("the shared chips go through ToneBadge", async () => {
    for (const name of ["cases-shared.tsx", "case-loop.tsx", "loop/DecisionReason.tsx"]) {
      expect(await readFile(path.join(CTEM, name), "utf8"), name).toMatch(/<ToneBadge\b/);
    }
  });
});
