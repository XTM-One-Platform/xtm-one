/**
 * Every animation the app names has keyframes behind it - `yarn test`.
 *
 * An arbitrary animation (`animate-[orbit_40s_linear_infinite]`) and an
 * `--animate-*` theme token name their keyframes as a plain string. Tailwind
 * emits the rule whether or not `@keyframes orbit` exists, and a browser
 * silently draws nothing for an unknown name: no error, no warning, just a
 * still element. That is how the dashboard's ecosystem orbit stopped turning
 * when the `tailwind.config.ts` keyframes moved to `index.css` and `orbit` was
 * left behind.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`); under jsdom `import.meta.url`
// is not a file: URL, so the tree is found from the working directory.
const SRC = join(process.cwd(), "src") + sep;
const read = (path: string) => readFileSync(join(SRC, path), "utf8");

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules") continue;
      yield full;
      yield* sources(full);
    }
  }
}

function files(): { path: string; text: string }[] {
  const out: { path: string; text: string }[] = [];
  for (const dir of [SRC, ...sources(SRC)]) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isFile() || !/\.(css|tsx?)$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) continue;
      const path = join(dir, entry.name);
      out.push({ path: path.slice(SRC.length), text: readFileSync(path, "utf8") });
    }
  }
  return out;
}

const ALL = files();

/** Keyframes declared anywhere in the app: stylesheets and inline `<style>` blocks. */
const declared = new Set(ALL.flatMap(({ text }) => [...text.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1])));

/** Animation names a class string or a theme token refers to. */
function referenced(): { name: string; where: string }[] {
  const refs: { name: string; where: string }[] = [];
  for (const { path, text } of ALL) {
    // `animate-[name_3s_linear_infinite,other_1s]`: the first word of each comma-separated animation.
    for (const m of text.matchAll(/animate-\[([^\]\s"'`]+)\]/g)) {
      for (const part of m[1].split(",")) refs.push({ name: part.split("_")[0], where: path });
    }
    // `--animate-name: keyframes 3s ...` theme tokens.
    for (const m of text.matchAll(/--animate-[\w-]+:\s*([\w-]+)/g)) refs.push({ name: m[1], where: path });
  }
  return refs;
}

test("every animation named by an arbitrary class or a theme token has its @keyframes", () => {
  const refs = referenced();
  // The scan must see the orbit itself, or its patterns have gone stale.
  expect(refs.map(({ name }) => name)).toContain("orbit");
  const missing = refs.filter(({ name }) => !declared.has(name)).map(({ name, where }) => `${name} (${where})`);
  expect(missing).toEqual([]);
});

test("the ecosystem orbit turns clockwise and its logos turn back at the ring's speed", () => {
  const css = read("index.css");
  expect(css).toMatch(/@keyframes orbit \{\s*from \{ transform: rotate\(0deg\); \}\s*to \{ transform: rotate\(360deg\); \}\s*\}/);
  expect(css).toMatch(/@keyframes counter-orbit \{\s*from \{ transform: rotate\(0deg\); \}\s*to \{ transform: rotate\(-360deg\); \}\s*\}/);
  // The ring of logos and the counter-rotation of each logo share 40s, so the logos stay upright.
  expect(css).toMatch(/--animate-counter-orbit: counter-orbit 40s linear infinite;/);
  // The home dashboard's ecosystem orbit and the CTEM posture orbit (restored in the
  // command center hero, #11687) turn the same way.
  for (const file of ["pages/Dashboard.tsx", "pages/xtm-one/ctem/ctem-viz.tsx"]) {
    const source = read(file);
    expect(source, file).toMatch(/animate-\[orbit_40s_linear_infinite\] motion-reduce:animate-none/);
    expect(source, file).toMatch(/animate-counter-orbit motion-reduce:animate-none"/);
    expect(source, file).toMatch(/animate-\[orbit_60s_linear_infinite\] motion-reduce:animate-none/);
    // A delay or a duration on a logo puts it out of phase with its carrier, which has none:
    // logo i would stay tilted by its share of the turn instead of upright.
    const logo = source.match(/animate-counter-orbit motion-reduce:animate-none"\s*\n\s*style=\{\{([^}]*)\}\}/);
    expect(logo, file).not.toBeNull();
    expect(logo?.[1], file).not.toMatch(/animation(Delay|Duration)/);
  }
});
