// A divider with no label is one continuous rule: no second segment and no
// gap at its centre where the label would sit (#10352). A labelled divider
// keeps its two segments around the label.
//
// happy-dom lays nothing out: what is pinned here is the marker the view
// publishes (`data-labelled`) and the stylesheet rule that drops the second
// segment of an unlabelled divider.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckBlock } from "@/types";
import { BlockList } from "./index";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

function renderDivider(block: DeckBlock): HTMLElement {
  const { getByRole } = renderWithProviders(
    <div className="deck-root">
      <BlockList blocks={[block]} className="deck-blocks deck-blocks-slide" path="blocks" />
    </div>,
  );
  return getByRole("separator");
}

describe("the divider block", () => {
  it("without a label is marked unlabelled and holds no text", () => {
    const divider = renderDivider({ type: "divider" } as DeckBlock);
    expect(divider.dataset.labelled).toBeUndefined();
    expect(divider.textContent).toBe("");
  });

  it("with an empty label is unlabelled too", () => {
    const divider = renderDivider({ type: "divider", label: "" } as DeckBlock);
    expect(divider.dataset.labelled).toBeUndefined();
  });

  it("with a label is marked labelled and shows it", () => {
    const divider = renderDivider({ type: "divider", label: "Then" } as DeckBlock);
    expect(divider.dataset.labelled).toBe("true");
    expect(divider.textContent).toBe("Then");
  });

  it("drops the second segment when it has no label", () => {
    expect(stylesheet).toContain('.deck-root .deck-divider:not([data-labelled="true"])::after { display: none; }');
  });
});
