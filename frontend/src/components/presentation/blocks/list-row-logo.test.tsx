/**
 * A picture in place of a list or feature_list row's marker - `yarn test:dom`.
 *
 * `logo_file_id` on a `list` or `feature_list` item draws the file (an SVG
 * flag, a small logo) in the row's icon slot instead of its bullet, glyph or
 * tile, with an optional hairline (`logo_border`). A list with one such row
 * says so (`data-row-logos`) so the stylesheet aligns every row's text on one
 * edge; a list without one renders exactly as before. The geometry (cap
 * height, centring on the first line) is the stylesheet's and is checked in
 * the browser. Every asset is a generic placeholder.
 */
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckFeatureListBlock, DeckListBlock } from "@/types";

import { DeckAssetProvider } from "../asset-context";
import { FeatureListBlockView } from "./cards";
import { ListBlockView } from "./list";

vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

const FLAG = "00000000-0000-4000-8000-00000000f001";
const FLAG_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 3 2"><rect width="3" height="2" fill="#8a94a6"/></svg>';
const FLAG_SRC = `data:image/svg+xml;base64,${btoa(FLAG_SVG)}`;

function render(view: React.ReactNode) {
  return renderWithProviders(
    <div className="deck-root" data-scheme="light">
      <DeckAssetProvider value={() => FLAG_SRC}>{view}</DeckAssetProvider>
    </div>,
  );
}

describe("list row picture", () => {
  test("a row with logo_file_id draws the picture in the marker slot, bordered on request", () => {
    const block: DeckListBlock = {
      type: "list",
      items: [
        { text: "**Paris**\nFrance", logo_file_id: FLAG },
        { text: "**Tokyo**\nJapan", logo_file_id: FLAG, logo_border: true },
        { text: "No flag" },
      ],
    };
    const { container } = render(<ListBlockView block={block} />);
    const list = container.querySelector(".deck-list")!;
    expect(list.getAttribute("data-row-logos")).toBe("true");
    const rows = Array.from(list.querySelectorAll("li"));
    expect(rows.map((row) => row.getAttribute("data-logo"))).toEqual(["true", "true", null]);
    const logos = rows.map((row) => row.querySelector(".deck-list-marker > .deck-row-logo"));
    expect(logos[0]?.querySelector("img")?.getAttribute("src")).toBe(FLAG_SRC);
    expect(logos[0]?.hasAttribute("data-export-picture")).toBe(true);
    expect(logos[0]?.getAttribute("data-border")).toBeNull();
    expect(logos[1]?.getAttribute("data-border")).toBe("true");
    expect(logos[2]).toBeNull();
  });

  test("a list without the field renders as before", () => {
    const block: DeckListBlock = { type: "list", style: "check", items: [{ text: "One" }, { text: "Two", icon: "x" }] };
    const { container } = render(<ListBlockView block={block} />);
    const list = container.querySelector(".deck-list")!;
    expect(list.hasAttribute("data-row-logos")).toBe(false);
    expect(list.querySelector("[data-logo], .deck-row-logo")).toBeNull();
    expect(list.querySelectorAll(".deck-list-marker svg")).toHaveLength(2);
  });
});

describe("feature_list row picture", () => {
  test("a feature with logo_file_id and no icon draws the picture in place of the tile", () => {
    const block: DeckFeatureListBlock = {
      type: "feature_list",
      items: [
        { title: "Partner", logo_file_id: FLAG, logo_border: true },
        { title: "Feature", icon: "shield-check" },
      ],
    };
    const { container } = render(<FeatureListBlockView block={block} />);
    const rows = Array.from(container.querySelectorAll(".deck-feature"));
    expect(rows[0].querySelector(".deck-icon-tile")).toBeNull();
    expect(rows[0].querySelector(".deck-icon-text-icon > .deck-row-logo[data-border='true'] img")?.getAttribute("src")).toBe(FLAG_SRC);
    expect(rows[1].querySelector(".deck-icon-tile")).not.toBeNull();
    expect(rows[1].querySelector(".deck-row-logo")).toBeNull();
  });
});
