// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * Every `<Switch>` must have an accessible name.
 *
 * A switch with no name announces as "switch, on" — the state without the
 * subject, which is worse than useless on a settings page holding a dozen of
 * them. 104 of this app's 121 switches were in that state, and it went unnoticed
 * for the same reason it always does: the label sits right beside the control,
 * so the screen looks perfectly labelled.
 *
 * This is a static check rather than a rendered one on purpose. The browser
 * accessibility pass that found the icon-button debt could only see the pages it
 * rendered, and permission-gated UI does not render — two unnamed buttons behind
 * a `canManage` branch survived that audit and were caught weeks later by an
 * end-to-end test. Source has no such blind spot: a branch that never renders is
 * still in the file.
 *
 * What it accepts as a name:
 *
 *   - `aria-label` or `aria-labelledby` on the control;
 *   - a `title`, which is the accessible-name computation's last resort — weak,
 *     but the name is there;
 *   - an `id`, which a `<label for>` elsewhere can point at;
 *   - being inside a `<label>`, which names the first labelable element it
 *     contains — and a Radix switch is a `<button>`, which is labelable;
 *   - a `{...spread}` whose object is declared in the same file carrying one of
 *     those attributes. A settings row that builds `id` / `aria-labelledby` /
 *     `aria-describedby` once and spreads them onto whichever control the
 *     option needs is named; demanding an inline `aria-label` there would add a
 *     second name to drift from the visible one.
 *
 * What it cannot check is whether the name is the RIGHT one. `aria-label={label}`
 * pointing at the wrong variable passes here and still misleads a user. The
 * names in this codebase were taken from the visible text beside each control by
 * `scripts/fds-migration/name-switches.mjs`, whose proposals were read one by
 * one — five were wrong and fixed by hand. That review is not repeatable by a
 * test; keeping the count at zero is.
 */

const SRC = path.resolve(__dirname, "../src");

async function* walk(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (full.endsWith(".tsx")) yield full;
  }
}

/** End of the JSX opening tag at `start`, tracking braces and strings. */
function openingTagEnd(source: string, start: number): number {
  let depth = 0;
  let inString: string | null = null;
  for (let i = start; i < source.length; i++) {
    const c = source[i];
    if (inString) {
      if (c === inString) inString = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      inString = c;
      continue;
    }
    if (c === "{") depth += 1;
    else if (c === "}") depth -= 1;
    else if (c === ">" && depth === 0) return i;
  }
  return -1;
}

async function unnamedSwitches(): Promise<string[]> {
  const offenders: string[] = [];
  for await (const file of walk(SRC)) {
    const source = await readFile(file, "utf8");
    const regex = /<Switch[\s/>]/g;
    let match: RegExpExecArray | null;
    while ((match = regex.exec(source))) {
      const end = openingTagEnd(source, match.index);
      if (end === -1) continue;
      const tag = source.slice(match.index, end + 1);
      // `title` counts, as it does in the icon-button guard: it is the last
      // resort of the accessible-name computation, and two flow-builder
      // switches are named that way. Weak, but not missing.
      if (/aria-label|aria-labelledby|\bid=|title=/.test(tag)) continue;

      // A spread whose object is DECLARED in the file with a name on it
      // (`const labelled = { "aria-labelledby": ids.label, ... }`) is a name
      // this scan can follow, and refusing it would push the author to add a
      // redundant `aria-label` that then drifts from the visible label.
      const spread = tag.match(/\{\.\.\.(\w+)\}/)?.[1];
      if (spread) {
        const declared = source.match(
          new RegExp(`(?:const|let)\\s+${spread}\\s*[:=][^;]*?(?:aria-label|aria-labelledby)`, "s"),
        );
        if (declared) continue;
      }

      // Inside a <label>? Count openings against closings before this point.
      const before = source.slice(0, match.index);
      const opens = (before.match(/<label[\s>]/g) ?? []).length;
      const closes = (before.match(/<\/label>/g) ?? []).length;
      if (opens > closes) continue;

      const line = before.split("\n").length;
      offenders.push(`${path.relative(SRC, file)}:${line}`);
    }
  }
  return offenders;
}

describe("Switch accessible names", () => {
  it("gives every switch a name, or puts it inside a label", async () => {
    const offenders = await unnamedSwitches();
    expect(offenders, `switches with no accessible name:\n  ${offenders.join("\n  ")}`).toEqual([]);
  });

  it("checks a meaningful number of switches, so a broken matcher cannot pass", async () => {
    // A regression in the scan itself would report zero offenders out of zero
    // switches and look like success. Pin the population too.
    let total = 0;
    for await (const file of walk(SRC)) {
      total += ((await readFile(file, "utf8")).match(/<Switch[\s/>]/g) ?? []).length;
    }
    expect(total).toBeGreaterThan(100);
  });
});
