/**
 * Role and trace status badges are translated on every page - `yarn test`.
 *
 * The Users list, the group members list,
 * the trace list, the agent Observability tab and the trace peek drawer
 * printed the raw `admin` / `user` role and `ok` / `error` trace status.
 * Every page now goes through `roleLabelKey` and `traceStatusLabelKey`; the
 * grep gate below keeps a raw value from coming back in a JSX text node.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { roleLabelKey } from "./role-labels.ts";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const SRC = join(process.cwd(), "src");

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.tsx$/.test(entry.name)) {
      yield full;
    }
  }
}

test("shows a known role by its catalog key, an unknown one as written", () => {
  assert.equal(roleLabelKey("admin"), "Admin");
  assert.equal(roleLabelKey("user"), "User");
  assert.equal(roleLabelKey("auditor"), "auditor");
});

test("no page prints a raw role or trace status in a JSX text node", () => {
  const raw = />\{\s*(?:\w+\.)?(?:role|status)\s*\}</;
  const watched = /\{\s*(?:trace|t|u|m|member|user)\.(?:role|status)\s*\}/;
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    readFileSync(file, "utf8").split("\n").forEach((line, index) => {
      if (raw.test(line) && watched.test(line)) {
        offenders.push(`${relative(SRC, file).split(sep).join("/")}:${index + 1}`);
      }
    });
  }
  assert.deepEqual(offenders, [], `raw role or trace status rendered in: ${offenders.join(", ")}`);
});
