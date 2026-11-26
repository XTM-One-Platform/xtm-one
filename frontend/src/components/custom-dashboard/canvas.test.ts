/**
 * Unit tests for the canvas of an absolute layout - `yarn test:unit`.
 *
 * How a canvas is read (defensively), scaled to the screen, and every edit
 * the studio makes to it, each one definition in, one definition out, and the
 * operations that save the difference.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  addElement,
  addTab,
  alignItems,
  canvasScale,
  copyItems,
  definitionOperations,
  distributeItems,
  endPoint,
  blockMap,
  freshId,
  itemsIn,
  moveItems,
  moveTab,
  nearestPort,
  pasteItems,
  readCanvas,
  readElement,
  removeItems,
  removeTab,
  renameTab,
  reorderItems,
  resizeItems,
  setBox,
  setCanvasOptions,
  setElementOptions,
  setLineEnd,
  setTabOptions,
  setTabType,
  setTheme,
} from "./canvas.ts";
import { normalizeDashboard } from "./spec.ts";

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- the definitions read here are free JSON
type Raw = Record<string, any>;

const IMAGE = "3f2b8c1e-7d4a-4c55-9a8e-0b1c2d3e4f50";

function definition(): Raw {
  return {
    version: 2,
    title: "Ops room",
    dataSources: { ds_kpi: { type: "static", options: { shape: "number", data: { value: 3 } } } },
    visualizations: {
      kpi: { type: "xtm.singlevalue", title: "Open", dataSources: { primary: "ds_kpi" } },
      box: { type: "xtm.shape.rectangle", options: { fillColor: "#112233", rx: 6 } },
      dot: { type: "xtm.shape.ellipse", options: {} },
      arrow: { type: "xtm.shape.line", options: { toArrow: true } },
      map: { type: "xtm.image", options: { fileId: IMAGE, alt: "Network" } },
      brief: { type: "xtm.text", options: { body: "Hello" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "canvas", label: "Canvas" }, { layoutId: "grid", label: "Grid" }] },
      layoutDefinitions: {
        canvas: {
          type: "absolute",
          options: { width: 1000, height: 600, display: "fit-to-width", backgroundColor: "#000000" },
          structure: [
            { item: "box", type: "block", position: { x: 10, y: 20, w: 200, h: 100 } },
            { item: "dot", type: "block", position: { x: 400, y: 20, w: 80, h: 80 } },
            { item: "arrow", type: "line", position: { from: { item: "box", port: "e" }, to: { x: 300, y: 300 } } },
            { item: "kpi", type: "block", position: { x: 600, y: 300, w: 200, h: 100 } },
            { item: "map", type: "block", position: { x: 10, y: 300, w: 300, h: 200 } },
          ],
        },
        grid: { type: "grid", structure: [{ item: "brief", type: "block", position: { x: 0, y: 0, w: 12, h: 4 } }] },
      },
    },
  };
}

const last = <T>(list: T[]): T => list[list.length - 1];
const structure = (def: Raw, layoutId = "canvas") => def.layout.layoutDefinitions[layoutId].structure as Raw[];
const positionOf = (def: Raw, id: string, layoutId = "canvas") => structure(def, layoutId).find((e) => e.item === id)?.position;

test("a canvas tab is read in pixels: its options, its elements and its widgets in stacking order", () => {
  const dashboard = normalizeDashboard(definition());
  const [canvas, grid] = dashboard.tabs;
  assert.equal(canvas.type, "absolute");
  // The elements are no widgets; the data visualization is.
  assert.deepEqual(canvas.widgets.map((w) => [w.id, w.kind]), [["kpi", "data"]]);
  assert.deepEqual(canvas.canvas?.options, { width: 1000, height: 600, display: "fit-to-width", backgroundColor: "#000000", backgroundImage: undefined });
  assert.deepEqual(canvas.canvas?.items.map((item) => [item.id, item.type, item.type === "block" ? item.element?.kind ?? "widget" : "line"]), [
    ["box", "block", "rectangle"],
    ["dot", "block", "ellipse"],
    ["arrow", "line", "line"],
    ["kpi", "block", "widget"],
    ["map", "block", "image"],
  ]);
  assert.equal(grid.canvas, undefined);
  assert.deepEqual(grid.widgets.map((w) => w.id), ["brief"]);
});

test("the theme and the tab bar's position are read", () => {
  const raw = definition();
  raw.theme = "light";
  raw.layout.tabs.options = { barPosition: "left" };
  const dashboard = normalizeDashboard(raw);
  assert.equal(dashboard.theme, "light");
  assert.equal(dashboard.tabBarPosition, "left");
  assert.equal(normalizeDashboard({ ...definition(), theme: "sepia" }).theme, undefined);
});

test("elements are read defensively: unknown colours, ids and values are dropped or clamped", () => {
  assert.deepEqual(readElement("xtm.shape.rectangle", { fillColor: "red", rx: -4, strokeWidth: 999, fillOpacity: 4 }), {
    kind: "rectangle",
    strokeColor: undefined,
    strokeWidth: 50,
    strokeOpacity: 1,
    strokeDashStyle: "solid",
    fillColor: undefined,
    fillOpacity: 1,
    rx: 0,
  });
  assert.deepEqual(readElement("xtm.icon", { icon: "<script>", fileId: "../../etc" }), {
    kind: "icon", icon: undefined, fileId: undefined, color: undefined, opacity: 1, alt: undefined,
  });
  assert.equal(readElement("xtm.image", { fileId: IMAGE.toUpperCase() })?.kind, "image");
  assert.equal((readElement("xtm.image", { fileId: IMAGE.toUpperCase() }) as Raw).fileId, IMAGE);
  assert.equal(readElement("xtm.singlevalue", {}), undefined);
  // A line placed as a block, a block without a place and an unknown widget are left out.
  const model = readCanvas(
    { structure: [{ item: "arrow", position: { x: 0, y: 0, w: 1, h: 1 } }, { item: "box" }, { item: "kpi", position: { x: 0, y: 0, w: 1, h: 1 } }] },
    definition().visualizations,
    () => false,
  );
  assert.deepEqual(model.items, []);
  assert.deepEqual(model.options, { width: 1440, height: 960, display: "auto", backgroundColor: undefined, backgroundImage: undefined });
});

test("the canvas is scaled to its viewport by its display mode, or by the editor's zoom", () => {
  const canvas = { width: 1000, height: 500 };
  assert.equal(canvasScale({ ...canvas, display: "actual-size" }, { width: 500, height: 500 }), 1);
  assert.equal(canvasScale({ ...canvas, display: "fit-to-width" }, { width: 2000, height: 100 }), 2);
  assert.equal(canvasScale({ ...canvas, display: "fit-to-width" }, { width: 500, height: 100 }), 0.5);
  // Auto: down to the viewport, never up.
  assert.equal(canvasScale({ ...canvas, display: "auto" }, { width: 2000, height: 2000 }), 1);
  assert.equal(canvasScale({ ...canvas, display: "auto" }, { width: 800, height: 200 }), 0.4);
  assert.equal(canvasScale({ ...canvas, display: "auto" }, { width: 800, height: 200 }, 1.5), 1.5);
});

test("a held line end follows its block's port; a marquee picks what it touches; a port snaps within reach", () => {
  const model = normalizeDashboard(definition()).tabs[0].canvas!;
  const blocks = blockMap(model.items);
  assert.deepEqual(endPoint({ item: "box", port: "e" }, blocks), { x: 210, y: 70 });
  assert.deepEqual(endPoint({ item: "gone", port: "e" }, blocks), { x: 0, y: 0 });
  assert.deepEqual(itemsIn(model.items, { x: 0, y: 0, w: 250, h: 150 }), ["box", "arrow"]);
  assert.deepEqual(nearestPort(model.items, { x: 395, y: 58 }, 12), { item: "dot", port: "w" });
  assert.equal(nearestPort(model.items, { x: 395, y: 58 }, 12, "dot"), null);
  assert.equal(nearestPort(model.items, { x: 900, y: 10 }, 12), null);
});

test("moving keeps blocks on the canvas and moves a line's free ends only", () => {
  const moved = moveItems(definition(), "canvas", ["box", "arrow"], 10, -50);
  assert.deepEqual(positionOf(moved, "box"), { x: 20, y: 0, w: 200, h: 100 });
  assert.deepEqual(positionOf(moved, "arrow"), { from: { item: "box", port: "e" }, to: { x: 310, y: 250 } });
  const far = moveItems(definition(), "canvas", ["dot"], 5000, 5000);
  assert.deepEqual(positionOf(far, "dot"), { x: 920, y: 520, w: 80, h: 80 });
  // Nothing else changed, and the input is untouched.
  assert.deepEqual(positionOf(moved, "dot"), positionOf(definition(), "dot"));
  assert.equal(moveItems(definition(), "canvas", [], 1, 1).title, "Ops room");
});

test("resizing, setting a box and a line end", () => {
  assert.deepEqual(positionOf(resizeItems(definition(), "canvas", ["box"], -500, 10), "box"), { x: 10, y: 20, w: 1, h: 110 });
  assert.deepEqual(positionOf(setBox(definition(), "canvas", "box", { x: -5, y: 7.6, w: 300, h: 50 }), "box"), { x: 0, y: 8, w: 300, h: 50 });
  const held = setLineEnd(definition(), "canvas", "arrow", "to", { item: "dot", port: "w" });
  assert.deepEqual(positionOf(held, "arrow"), { from: { item: "box", port: "e" }, to: { item: "dot", port: "w" } });
  const loose = setLineEnd(definition(), "canvas", "arrow", "from", { x: -3, y: 9999 });
  assert.deepEqual(positionOf(loose, "arrow").from, { x: 0, y: 600 });
});

test("aligning and distributing blocks", () => {
  const left = alignItems(definition(), "canvas", ["box", "dot", "kpi"], "left");
  assert.deepEqual(["box", "dot", "kpi"].map((id) => positionOf(left, id).x), [10, 10, 10]);
  const middle = alignItems(definition(), "canvas", ["box", "kpi"], "middle");
  assert.deepEqual(["box", "kpi"].map((id) => positionOf(middle, id).y), [160, 160]);
  const right = alignItems(definition(), "canvas", ["box", "dot"], "right");
  assert.deepEqual(["box", "dot"].map((id) => positionOf(right, id).x), [280, 400]);
  // One block alone is not aligned.
  assert.deepEqual(alignItems(definition(), "canvas", ["box"], "left"), definition());
  const spread = distributeItems(definition(), "canvas", ["box", "dot", "kpi"], "horizontal");
  // Span 10..800, blocks 200 + 80 + 200 wide: a gap of 155.
  assert.deepEqual(["box", "dot", "kpi"].map((id) => positionOf(spread, id).x), [10, 365, 600]);
  assert.deepEqual(distributeItems(definition(), "canvas", ["box", "dot"], "vertical"), definition());
});

test("layering: to the front, to the back, one step", () => {
  const order = (def: Raw) => structure(def).map((e) => e.item);
  assert.deepEqual(order(reorderItems(definition(), "canvas", ["box"], "front")), ["dot", "arrow", "kpi", "map", "box"]);
  assert.deepEqual(order(reorderItems(definition(), "canvas", ["map"], "back")), ["map", "box", "dot", "arrow", "kpi"]);
  assert.deepEqual(order(reorderItems(definition(), "canvas", ["dot"], "forward")), ["box", "arrow", "dot", "kpi", "map"]);
  assert.deepEqual(order(reorderItems(definition(), "canvas", ["box"], "backward")), ["box", "dot", "arrow", "kpi", "map"]);
});

test("removing lets go of the lines held to it and drops the data source only it read", () => {
  const removed = removeItems(definition(), ["box", "kpi"]) as Raw;
  assert.deepEqual(Object.keys(removed.visualizations), ["dot", "arrow", "map", "brief"]);
  assert.deepEqual(positionOf(removed, "arrow"), { from: { x: 210, y: 70 }, to: { x: 300, y: 300 } });
  assert.deepEqual(removed.dataSources, {});
  assert.deepEqual(structure(removed).map((e) => e.item), ["dot", "arrow", "map"]);
});

test("adding an element puts it on top, with a fresh id and its first options", () => {
  const { definition: added, id } = addElement(definition(), "canvas", "rectangle", { x: 950, y: 10 }) as { definition: Raw; id: string };
  assert.equal(id, "rectangle");
  assert.deepEqual(added.visualizations.rectangle, { type: "xtm.shape.rectangle", options: { fillOpacity: 0.15, strokeWidth: 2, rx: 8 } });
  assert.deepEqual(last(structure(added)), { item: "rectangle", type: "block", position: { x: 760, y: 10, w: 240, h: 140 } });
  const line = addElement(added, "canvas", "line", { x: 10, y: 10 }) as { definition: Raw; id: string };
  assert.deepEqual(last(structure(line.definition)), { item: "line", type: "line", position: { from: { x: 10, y: 10 }, to: { x: 210, y: 10 } } });
  const note = addElement(definition(), "canvas", "markdown", { x: 0, y: 0 }, "## Hello") as { definition: Raw; id: string };
  assert.deepEqual(note.definition.visualizations.markdown.options, { markdown: "## Hello" });
  assert.equal(freshId(new Set(["box", "box_2"]), "box"), "box_3");
  assert.equal(freshId(new Set(), "-!x"), "x");
});

test("element and canvas options are changed, a key set to undefined removed", () => {
  const recoloured = setElementOptions(definition(), "box", { fillColor: "#abcdef", rx: undefined }) as Raw;
  assert.deepEqual(recoloured.visualizations.box.options, { fillColor: "#abcdef" });
  const resized = setCanvasOptions(definition(), "canvas", { width: 1920, backgroundColor: undefined }) as Raw;
  assert.deepEqual(resized.layout.layoutDefinitions.canvas.options, { width: 1920, height: 600, display: "fit-to-width" });
});

test("copy and paste: new ids, offset places, lines held to their copies, the same or a new data source", () => {
  const clip = copyItems(definition(), "canvas", ["box", "arrow", "kpi"])!;
  assert.deepEqual(Object.keys(clip.dataSources), ["ds_kpi"]);
  const same = pasteItems(definition(), "canvas", clip) as { definition: Raw; ids: string[] };
  assert.deepEqual(same.ids, ["box_2", "arrow_2", "kpi_2"]);
  assert.deepEqual(positionOf(same.definition, "box_2"), { x: 30, y: 40, w: 200, h: 100 });
  assert.deepEqual(positionOf(same.definition, "arrow_2"), { from: { item: "box_2", port: "e" }, to: { x: 320, y: 320 } });
  assert.deepEqual(same.definition.visualizations.kpi_2.dataSources, { primary: "ds_kpi" });
  assert.deepEqual(Object.keys(same.definition.dataSources), ["ds_kpi"]);
  const fresh = pasteItems(definition(), "canvas", clip, { newDataSources: true }) as { definition: Raw; ids: string[] };
  assert.deepEqual(fresh.definition.visualizations.kpi_2.dataSources, { primary: "ds_kpi_2" });
  assert.deepEqual(fresh.definition.dataSources.ds_kpi_2, definition().dataSources.ds_kpi);
  // A line copied without the block it holds to lets go of it.
  const alone = pasteItems(definition(), "canvas", copyItems(definition(), "canvas", ["arrow"])!) as { definition: Raw; ids: string[] };
  assert.deepEqual(positionOf(alone.definition, "arrow_2"), { from: { x: 230, y: 90 }, to: { x: 320, y: 320 } });
  assert.equal(copyItems(definition(), "canvas", ["nothing"]), null);
});

test("tabs: add, rename, move, remove (with what only it placed), the bar's options", () => {
  const added = addTab(definition(), "absolute", "Wall")! as { definition: Raw; layoutId: string };
  assert.equal(added.layoutId, "layout_3");
  assert.deepEqual(added.definition.layout.layoutDefinitions.layout_3, { type: "absolute", options: { width: 1440, height: 960 }, structure: [] });
  assert.deepEqual(last(added.definition.layout.tabs.items), { layoutId: "layout_3", label: "Wall" });
  const renamed = renameTab(definition(), "grid", "  Notes  ") as Raw;
  assert.equal(renamed.layout.tabs.items[1].label, "Notes");
  assert.deepEqual(renameTab(definition(), "grid", "   "), definition());
  assert.deepEqual((moveTab(definition(), "grid", -1) as Raw).layout.tabs.items.map((t: Raw) => t.layoutId), ["grid", "canvas"]);
  assert.deepEqual(moveTab(definition(), "grid", 1), definition());
  const removed = removeTab(definition(), "canvas")! as Raw;
  assert.deepEqual(Object.keys(removed.layout.layoutDefinitions), ["grid"]);
  assert.deepEqual(Object.keys(removed.visualizations), ["brief"]);
  assert.deepEqual(removed.dataSources, {});
  assert.equal(removeTab(removed, "grid"), null);
  const options = setTabOptions(definition(), { showTabBar: false, barPosition: "left" }) as Raw;
  assert.deepEqual(options.layout.tabs.options, { showTabBar: false, barPosition: "left" });
  let full: Raw = definition();
  for (let i = 0; i < 8; i += 1) full = (addTab(full, "grid", `T${i}`) as { definition: Raw }).definition;
  assert.equal(addTab(full, "grid", "Eleventh"), null);
});

test("a tab switches between a grid and a canvas; a canvas holding elements stays one", () => {
  const canvas = setTabType(definition(), "grid", "absolute")! as Raw;
  assert.deepEqual(canvas.layout.layoutDefinitions.grid, {
    type: "absolute",
    options: { width: 1440, height: 960 },
    structure: [{ item: "brief", type: "block", position: { x: 0, y: 0, w: 1440, h: 328 } }],
  });
  const back = setTabType(canvas, "grid", "grid")! as Raw;
  assert.deepEqual(back.layout.layoutDefinitions.grid.structure, [{ item: "brief", type: "block", position: { x: 0, y: 0, w: 12, h: 4 } }]);
  assert.equal(setTabType(definition(), "canvas", "grid"), null);
});

test("the difference of two definitions is the set and delete operations that save it", () => {
  const before = definition();
  assert.deepEqual(definitionOperations(before, before), []);
  const after = setTheme(removeItems(moveItems(before, "canvas", ["dot"], 5, 5), ["kpi"]), "dark");
  assert.deepEqual(definitionOperations(before, after), [
    { op: "delete", path: "visualizations.kpi" },
    { op: "delete", path: "dataSources.ds_kpi" },
    { op: "set", path: "layout", value: after.layout },
    { op: "set", path: "theme", value: "dark" },
  ]);
  assert.deepEqual(definitionOperations(after, setTheme(after, null)), [{ op: "delete", path: "theme" }]);
  const recoloured = setElementOptions(before, "box", { fillColor: "#ffffff" }) as Raw;
  assert.deepEqual(definitionOperations(before, recoloured), [{ op: "set", path: "visualizations.box", value: recoloured.visualizations.box }]);
});

test("previews move, resize and re-hold items without touching the definition", async () => {
  const { shiftItems, withBox, withLineEnd, resizeBox, placeBlocks } = await import("./canvas.ts");
  const model = normalizeDashboard(definition()).tabs[0].canvas!;
  const shifted = shiftItems(model.items, new Set(["box", "arrow"]), 5, 6);
  assert.deepEqual(shifted.find((i) => i.id === "box"), { ...model.items[0], x: 15, y: 26 });
  assert.deepEqual((shifted.find((i) => i.id === "arrow") as Raw).to, { x: 305, y: 306 });
  assert.deepEqual((shifted.find((i) => i.id === "arrow") as Raw).from, { item: "box", port: "e" });
  assert.deepEqual(withBox(model.items, "dot", { x: 1, y: 2, w: 3, h: 4 }).find((i) => i.id === "dot"), { ...model.items[1], x: 1, y: 2, w: 3, h: 4 });
  assert.deepEqual((withLineEnd(model.items, "arrow", "from", { x: 9, y: 9 }).find((i) => i.id === "arrow") as Raw).from, { x: 9, y: 9 });
  const box = { x: 100, y: 100, w: 50, h: 40 };
  assert.deepEqual(resizeBox(box, "se", 10, 20), { x: 100, y: 100, w: 60, h: 60 });
  assert.deepEqual(resizeBox(box, "nw", 10, 10), { x: 110, y: 110, w: 40, h: 30 });
  assert.deepEqual(resizeBox(box, "w", 100, 0), { x: 142, y: 100, w: 8, h: 40 });
  const placed = placeBlocks(definition(), "grid", [{ id: "brief", x: 6, y: 1, w: 6, h: 3 }]);
  assert.deepEqual(positionOf(placed, "brief", "grid"), { x: 6, y: 1, w: 6, h: 3 });
});
