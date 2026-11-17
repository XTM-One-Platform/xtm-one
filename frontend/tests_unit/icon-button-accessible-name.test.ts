// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * A button whose children are icons and nothing else must carry an accessible
 * name.
 *
 * This is the check that was missing when the migration claimed no unnamed icon
 * buttons remained. That claim came from an axe run in a real browser, which is
 * authoritative about what it sees and silent about what it does not: axe can
 * only judge rendered DOM, and UI behind a permission branch never rendered. Two
 * unnamed buttons on the agent header survived it and were found weeks later by
 * an end-to-end test — one of them the delete button, with no name of any kind.
 * Source has no blind spot: a branch that never renders is still in the file.
 *
 * It is deliberately SOUND rather than COMPLETE. It only reports a button whose
 * body holds no text and no expression at all, because that is the one shape
 * where "it has no name from its content" is a fact rather than a guess. A
 * button rendering `{icon}` or `{label}` might be either, and an earlier version
 * that tried to judge those reported 592 sites, nearly all of them fine — a
 * check nobody would ever run twice. Narrowing it to the provable shape found
 * 102 real ones.
 *
 * A tooltip does not count, and that is the point worth remembering: it is
 * announced as a description after the name, or not at all, so a button whose
 * only text is its tooltip announces as "button". 34 of the 102 were in exactly
 * that state, which is why their tooltips now do double duty as labels.
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

async function scan(): Promise<{ offenders: string[]; iconOnly: number }> {
  const offenders: string[] = [];
  let iconOnly = 0;

  for await (const file of walk(SRC)) {
    const source = await readFile(file, "utf8");
    for (const tag of ["Button", "button"]) {
      const regex = new RegExp(`<${tag}[\\s]`, "g");
      let match: RegExpExecArray | null;
      while ((match = regex.exec(source))) {
        const tagEnd = openingTagEnd(source, match.index);
        if (tagEnd === -1) continue;
        const open = source.slice(match.index, tagEnd + 1);
        if (open.trimEnd().endsWith("/>")) continue;

        const close = source.indexOf(`</${tag}>`, tagEnd);
        if (close === -1) continue;
        const body = source.slice(tagEnd + 1, close);

        const withoutTags = body.replace(/<[^>]*>/g, " ");
        // Only ICONS count as "no name from content". An earlier version treated
        // any capitalised component as one, and reported the integration
        // marketplace's provider card — a `<button>` whose whole body is
        // `<ProviderCardBody item={item} />`, which renders the provider's name.
        // A child component can render text; an icon cannot. So the icons are
        // taken from what the file imports from lucide-react, which is a fact
        // rather than a guess about capitalisation.
        const iconNames = new Set(
          [...source.matchAll(/import\s*\{([^}]*)\}\s*from\s*"lucide-react"/g)]
            .flatMap((m) => m[1].split(","))
            .map((name) => name.trim().split(/\s+as\s+/).pop()!)
            .filter(Boolean),
        );
        const components = [...body.matchAll(/<([A-Z]\w*)/g)].map((m) => m[1]);
        const bodyIsPureJsx =
          !withoutTags.includes("{") &&
          !withoutTags.replace(/\s+/g, " ").trim() &&
          (/<svg/.test(body) || (components.length > 0 && components.every((c) => iconNames.has(c))));
        if (!bodyIsPureJsx) continue;

        iconOnly += 1;
        // `title` counts: it names a control when nothing else does. A tooltip
        // component wrapping the button does NOT — it is a description.
        if (/aria-label|aria-labelledby|sr-only|title=/.test(open + body)) continue;

        const line = source.slice(0, match.index).split("\n").length;
        offenders.push(`${path.relative(SRC, file)}:${line}`);
      }
    }
  }
  return { offenders, iconOnly };
}

describe("icon-only button accessible names", () => {
  it("names every button whose content is icons only", async () => {
    const { offenders } = await scan();
    expect(offenders, `icon-only buttons with no accessible name:\n  ${offenders.join("\n  ")}`).toEqual([]);
  });

  it("still recognises the shape it is meant to police", async () => {
    // Zero offenders out of zero buttons would look like success. Pin the
    // population so a regression in the matcher fails instead of passing.
    const { iconOnly } = await scan();
    expect(iconOnly).toBeGreaterThan(80);
  });
});
