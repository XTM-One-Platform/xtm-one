/**
 * The deck theme on a Mermaid flowchart - `yarn test:dom`.
 *
 * Dark and light, on `flowchart LR A-->|x|B-->|y|C-->|z|D`, drawn by the real
 * Mermaid under the component's own configuration in happy-dom: the theme the
 * deck derives from its tokens (`deckDiagramTheme` - the palette as Mermaid's
 * variables, the deck's rules Mermaid compiles into the drawing's stylesheet,
 * the geometry) and what `fitDeckGeometry` then writes on the laid-out
 * drawing (every node rounded to the deck radius, every edge label's
 * background grown into a pill), pinned per scheme.
 *
 * Three things the environment decides, none of them the theme:
 * - happy-dom measures no text (`getBBox()` is 0x0), so every box is Mermaid's
 *   padding around an empty label and a pill is the padding alone - the
 *   snapshot pins the attributes written, never a layout;
 * - happy-dom's HTML parser drops everything after a `<style>` inside an
 *   `<svg>`, which is what Mermaid's strict mode re-parses its drawing
 *   through (DOMPurify), so `render` answers an empty string here: the test
 *   reads the drawing Mermaid made in the DOM instead, kept through the
 *   mutation records of the container it was drawn in, with its stylesheet;
 * - there is no canvas, so a `color-mix()` token (the dark surface, both
 *   schemes' line) reads as its fallback, the way `paintedColor` degrades in a
 *   browser without one.
 */
import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it } from "vitest";

import { designTokenStylesheet } from "@/testing/design-tokens";

import { MERMAID_CONFIG, deckDiagramTheme, fitDeckGeometry } from "./diagram-theme";

const SOURCE = "flowchart LR\n  A-->|x|B-->|y|C-->|z|D";
const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

beforeAll(() => {
  const style = document.createElement("style");
  // The deck palette names design-system tokens the test document does not load.
  style.textContent = designTokenStylesheet() + stylesheet;
  document.head.appendChild(style);
});

/**
 * A deck root in the given scheme. The theme is read off the root itself: a
 * browser hands `Diagram`'s figure the inherited tokens, happy-dom's computed
 * style answers a custom property only where it is declared.
 */
function host(scheme: "dark" | "light"): HTMLElement {
  const root = document.createElement("div");
  root.className = "deck-root";
  root.dataset.deckTheme = scheme;
  document.body.appendChild(root);
  return root;
}

/** The `<svg>` Mermaid drew in `container`, held past Mermaid's own removal of it. */
async function drawnBy(render: () => Promise<unknown>, container: HTMLElement): Promise<SVGSVGElement> {
  const added: Node[] = [];
  const collect = (records: MutationRecord[]) => added.push(...records.flatMap((record) => Array.from(record.addedNodes)));
  const observer = new MutationObserver(collect);
  observer.observe(container, { childList: true });
  await render();
  collect(observer.takeRecords());
  observer.disconnect();
  const drawing = added.find((node): node is Element => node instanceof Element);
  const root = drawing?.querySelector("svg");
  if (!root) throw new Error("Mermaid drew nothing in the container");
  return root;
}

const attrs = <K extends string>(element: Element, names: readonly K[]): Record<K, string | null> =>
  Object.fromEntries(names.map((name) => [name, element.getAttribute(name)])) as Record<K, string | null>;

/** The flowchart as the deck draws it in `scheme`: the theme, the drawing's stylesheet and the geometry written on it. */
async function drawn(scheme: "dark" | "light") {
  const mermaid = (await import("mermaid")).default;
  const theme = deckDiagramTheme(host(scheme));
  mermaid.initialize({ ...MERMAID_CONFIG, themeVariables: theme.themeVariables, themeCSS: theme.themeCSS });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const id = `deck-diagram-test-${scheme}`;
  const root = await drawnBy(() => mermaid.render(id, SOURCE, container), container);
  fitDeckGeometry(root, theme.geometry);
  const compiled = root.querySelector("style")?.textContent ?? "";
  const nodes = Array.from(root.querySelectorAll(".node")).map((node) => ({
    id: node.id.replace(`${id}-flowchart-`, ""),
    label: node.textContent?.trim(),
    rect: attrs(node.querySelector("rect.label-container") as Element, ["rx", "ry", "x", "y", "width", "height"]),
  }));
  const pills = Array.from(root.querySelectorAll(".edgeLabel rect.background[width]")).map((rect) => ({
    label: rect.parentElement?.textContent?.trim(),
    ...attrs(rect, ["x", "y", "width", "height", "rx", "ry"]),
  }));
  return { id, theme, compiled, root, nodes, pills };
}

describe("the deck theme on a Mermaid flowchart", () => {
  it.each(["dark", "light"] as const)("draws flowchart LR A-->|x|B-->|y|C-->|z|D in the %s scheme", async (scheme) => {
    const { id, theme, compiled, root, nodes, pills } = await drawn(scheme);
    expect({ theme, nodes, pills }).toMatchSnapshot();
    // Mermaid compiled the deck's rules into the drawing's own stylesheet, under its id, after its own
    // (stylis drops the spaces and the quotes around a font name on the way).
    const tight = (css: string) => css.replace(/\s+|"/g, "");
    for (const rule of theme.themeCSS.split("\n")) {
      const [selectors, declarations] = rule.split(" { ");
      for (const selector of selectors.split(", ")) expect(tight(compiled)).toContain(tight(`#${id} ${selector}`));
      for (const declaration of declarations.replace(/; }$/, "").split("; ")) expect(tight(compiled)).toContain(tight(declaration));
    }
    const accent = theme.themeVariables.arrowheadColor as string;
    const markerRules = tight(compiled).split(`#${id}.marker{fill:`).slice(1);
    expect(markerRules.length).toBeGreaterThan(1);
    expect(markerRules[markerRules.length - 1].startsWith(tight(accent))).toBe(true);
    // Four rounded nodes, three pills with round ends, arrowheads to colour.
    expect(nodes).toHaveLength(4);
    expect(nodes.every((node) => node.rect.rx === "12" && node.rect.ry === "12")).toBe(true);
    expect(pills.map((pill) => pill.label)).toEqual(["x", "y", "z"]);
    expect(pills.every((pill) => Number(pill.rx) === Number(pill.height) / 2 && pill.ry === pill.rx)).toBe(true);
    expect(root.querySelectorAll("marker.marker .arrowMarkerPath").length).toBeGreaterThan(0);
  }, 30_000);

  it("reads the scheme's own palette: the dark and the light drawing differ in colour and in nothing else", async () => {
    const dark = await drawn("dark");
    const light = await drawn("light");
    expect(dark.theme.themeVariables.darkMode).toBe(true);
    expect(light.theme.themeVariables.darkMode).toBe(false);
    expect(dark.theme.themeVariables.mainBkg).not.toBe(light.theme.themeVariables.mainBkg);
    expect(dark.theme.themeVariables.arrowheadColor).not.toBe(light.theme.themeVariables.arrowheadColor);
    expect(dark.theme.geometry).toEqual(light.theme.geometry);
    expect(dark.nodes.map((node) => node.rect)).toEqual(light.nodes.map((node) => node.rect));
    expect(dark.pills).toEqual(light.pills);
  }, 30_000);
});
