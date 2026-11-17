// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * A field must read against the surface it sits on, at every elevation layer.
 *
 * Since @filigran/design-system 1.3.0 the library re-declares the input
 * backgrounds on every `.layer-N` itself (CHANGELOG e6b6414), so the host copy
 * index.css carried for F31 is gone. This models the cascade the way a browser
 * computes it, from the installed token sheet and `index.css`, and fails if a
 * field ever paints its surface's own colour again, or if the host starts
 * re-declaring input tokens on a layer (a token belongs to the library).
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");
const HOST = path.join(FRONTEND, "src/index.css");
const LAYERS = [0, 1, 2, 3] as const;
const MODES = ["dark", "light"] as const;

type Scope = Record<string, string>;

/** An element of the model: what it declares itself, and where it inherits the rest from. */
interface Node {
  declared: Scope;
  parent?: Node;
}

/** Every custom property the rules naming `selector` in their selector list declare, in source order. */
function declarations(css: string, selector: string): Scope {
  const scope: Scope = {};
  const source = css.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const [, selectors, body] of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!selectors.split(",").some((s) => s.trim() === selector)) continue;
    for (const [, name, value] of body.matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) scope[name] = value.trim();
  }
  return scope;
}

/** A custom property's computed value on `node`: its own declaration with every `var()` resolved on `node`, else its parent's. */
function read(node: Node, name: string): string {
  const own = node.declared[name];
  if (own === undefined) {
    expect(node.parent, `--${name} is not declared`).toBeDefined();
    return read(node.parent!, name);
  }
  return own.replace(/var\(--([\w-]+)\)/g, (_, ref: string) => read(node, ref));
}

async function sheets() {
  const theme = await readFile(THEME, "utf8");
  const host = await readFile(HOST, "utf8");
  const primitives: Scope = Object.fromEntries(
    [...theme.matchAll(/--([a-z]+-\d+):\s*(#[0-9a-fA-F]{6})\b/g)].map(([, name, value]) => [name, value.toLowerCase()]),
  );
  const root = declarations(theme, ":root");
  const library = Object.fromEntries(LAYERS.map((n) => [n, declarations(theme, `.layer-${n}`)])) as Record<number, Scope>;
  const hostLayers = Object.fromEntries(LAYERS.map((n) => [n, declarations(host, `.layer-${n}`)])) as Record<number, Scope>;
  const html = (mode: (typeof MODES)[number]): Node => ({
    declared: { ...primitives, ...root, ...declarations(theme, `.${mode}`) },
  });
  const surface = (mode: (typeof MODES)[number], layer: number): Node => ({
    declared: { ...library[layer], ...hostLayers[layer] },
    parent: html(mode),
  });
  return { hostLayers, surface };
}

describe("input backgrounds follow the elevation layer", () => {
  it("paints a field apart from its surface, at every layer, in both themes", async () => {
    const { surface } = await sheets();
    for (const mode of MODES) {
      for (const layer of LAYERS) {
        const on = surface(mode, layer);
        const where = `layer-${layer}, ${mode}`;
        expect(read(on, "bg-input-default"), `field fill, ${where}`).toBe(read(on, "bg-elevation-highlight"));
        expect(read(on, "bg-input-hover"), `field hover, ${where}`).toBe(read(on, "bg-elevation-hover"));
        expect(read(on, "bg-input-disabled"), `field disabled, ${where}`).toBe(read(on, "bg-elevation-disabled"));
        expect(read(on, "bg-input-default"), `field against its surface, ${where}`).not.toBe(read(on, "bg-elevation-default"));
      }
    }
  });

  it("leaves the input tokens of every layer to the library", async () => {
    const { hostLayers } = await sheets();
    for (const layer of LAYERS) {
      const inputs = Object.keys(hostLayers[layer]).filter((name) => name.includes("-input-"));
      expect(inputs, `index.css re-declares input tokens on .layer-${layer}`).toEqual([]);
    }
  });
});
