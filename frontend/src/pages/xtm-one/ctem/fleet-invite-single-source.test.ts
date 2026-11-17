// UX round 1 on #10515, B1: the fleets invitation is one component
// (CtemFleetInvite.tsx), drawn by Fleet activity and by the fleets row. Its
// wording written in a second source file means a second invitation that can
// drift (title size, control height, frame); this gate fails on it.

import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";
import { expect, test } from "vitest";

// vitest runs from frontend/ (CI and `yarn test`).
const SRC = join(process.cwd(), "src") + sep;

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* sources(path);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) yield path;
  }
}

const INVITE_WORDING = [
  "Add agentic work to this page",
  "Close the fleets invitation",
  "Fleets are off: this page reads your connected modules only.",
];

test("the fleets invitation is written in CtemFleetInvite.tsx only", () => {
  const owners = new Map<string, string[]>();
  for (const path of sources(SRC)) {
    const text = readFileSync(path, "utf8");
    for (const wording of INVITE_WORDING) {
      if (text.includes(`"${wording}`)) owners.set(wording, [...(owners.get(wording) ?? []), path.slice(SRC.length).split(sep).join("/")]);
    }
  }
  for (const wording of INVITE_WORDING) {
    expect(owners.get(wording), wording).toEqual(["pages/xtm-one/ctem/CtemFleetInvite.tsx"]);
  }
});

test("Fleet activity and the fleets row both draw the shared invitation", () => {
  for (const file of ["FleetActivityCard.tsx", "FleetsRowCard.tsx"]) {
    const text = readFileSync(join(SRC, "pages", "xtm-one", "ctem", file), "utf8");
    expect(text, file).toMatch(/<CtemFleetInvite\b/);
    expect(text, file).toMatch(/<CtemFleetInviteClosed\b/);
  }
});
