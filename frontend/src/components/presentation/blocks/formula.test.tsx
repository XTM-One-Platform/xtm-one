/**
 * The `formula` and `config_cards` blocks - `yarn test:dom`.
 *
 * A dimmed configuration row inks its logo in the muted colour (the row
 * carries the `data-logo-tone` the logo's ink layer keys on), and a logo of a
 * formula term's row shows its light variant on a light slide. Every file id
 * is a generic placeholder.
 */
import { readFileSync } from "node:fs";
import { describe, expect, test } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckConfigCardsBlock, DeckFormulaBlock, PresentationThemeConfig } from "@/types";

import { DeckAssetProvider } from "../asset-context";
import { SlideSchemeContext } from "../theme";
import { ConfigCardsBlockView, FormulaBlockView } from "./formula";

const DARK_LOGO = "00000000-0000-4000-8000-0000000000d1";
const LIGHT_LOGO = "00000000-0000-4000-8000-0000000000e1";
const OTHER_LOGO = "00000000-0000-4000-8000-0000000000f1";

// happy-dom computes no cascade: the stylesheet rules are pinned as written.
const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

function renderIn(scheme: "light" | "dark", view: React.ReactNode) {
  return renderWithProviders(
    <div className="deck-root" data-scheme={scheme}>
      <SlideSchemeContext.Provider value={{ scheme, overridden: false, tokens: {} as PresentationThemeConfig }}>
        <DeckAssetProvider value={(fileId) => `data:image/svg+xml,${fileId}`}>{view}</DeckAssetProvider>
      </SlideSchemeContext.Provider>
    </div>,
  );
}

describe("config_cards", () => {
  const block: DeckConfigCardsBlock = {
    type: "config_cards",
    cards: [
      {
        title: "Configuration",
        rows: [
          { logo_file_id: DARK_LOGO, label: "Module", chip: "Included" },
          { logo_file_id: OTHER_LOGO, label: "Module", chip: "Available, not used", state: "dimmed" },
          { label: "Text only", chip: "Available, not used", state: "dimmed" },
        ],
      },
      { title: "Other", rows: [{ label: "Module", chip: "Included" }] },
    ],
  };

  test("a dimmed row with a logo carries the muted logo tone, an active row none", () => {
    const { container } = renderIn("dark", <ConfigCardsBlockView block={block} />);
    const rows = container.querySelectorAll(".deck-config-row");
    expect(rows[0].getAttribute("data-logo-tone")).toBeNull();
    expect(rows[1].getAttribute("data-logo-tone")).toBe("muted");
    expect(rows[1].querySelector(".deck-logo-ink")).not.toBeNull();
    expect(rows[2].getAttribute("data-logo-tone")).toBeNull();
  });

  test("a tinted row's chip on a dark slide mixes its colour with the page ink", () => {
    const { container } = renderIn("dark", <ConfigCardsBlockView block={{ ...block, cards: [{ title: "T", rows: [{ label: "M", chip: "Included", chip_tone: "brand_1", tinted: true }] }, block.cards[1]] } as DeckConfigCardsBlock} />);
    expect(container.querySelector(".deck-config-row")?.getAttribute("data-tinted")).toBe("true");
    expect(stylesheet).toMatch(
      /\[data-scheme="dark"\] \.deck-config-row\[data-tinted="true"\] \.deck-config-chip \{\s*color: color-mix\(in srgb, var\(--tone\) 70%, var\(--deck-fg\)\);/,
    );
  });

  test("every card is a 3-row subgrid (head / rows / footer), so rows and footers line up across cards", () => {
    const { container } = renderIn("dark", <ConfigCardsBlockView block={{ ...block, cards: [{ ...block.cards[0], footer: "Capacity" }, block.cards[1]] } as DeckConfigCardsBlock} />);
    // The three subgrid tracks are the card's three direct children: head, rows, footer.
    const card = container.querySelector(".deck-config-card");
    expect([...(card?.children ?? [])].map((child) => child.tagName)).toEqual(["DIV", "UL", "P"]);
    expect(card?.lastElementChild?.textContent).toBe("Capacity");
    // UX round 5 B2: a title on two lines moved one card's first row 24 px down.
    expect(stylesheet).toMatch(/\.deck-root \.deck-config-cards \{[^}]*grid-template-rows: auto 1fr auto;/);
    expect(stylesheet).toMatch(
      /\.deck-root \.deck-config-card \{[^}]*grid-row: span 3;\s*grid-template-rows: subgrid;/,
    );
    // UX round 6 B1: the implicit column sized to max-content and a tinted row crossed the card's right border.
    expect(stylesheet).toMatch(/\.deck-root \.deck-config-card \{[^}]*grid-template-columns: minmax\(0, 1fr\);/);
  });
});

describe("config_cards fidelity", () => {
  const block: DeckConfigCardsBlock = {
    type: "config_cards",
    heading: "Example configurations",
    cards: [
      {
        title: "Configuration",
        rows: [
          { label: "Module", chip: "Enterprise Edition" },
          { label: "Other", chip: "Available, not used", state: "dimmed" },
        ],
        footer: "Capacity",
        footer_value: "Included allowance",
      },
      { title: "Other", rows: [{ label: "Module", chip: "Included" }] },
    ],
  };

  test("rows are framed by default, chips share one end column, the footer is a split band", () => {
    for (const scheme of ["light", "dark"] as const) {
      const { container } = renderIn(scheme, <ConfigCardsBlockView block={block} />);
      const figure = container.querySelector(".deck-config-cards");
      expect(figure?.getAttribute("data-row-style")).toBe("outlined");
      expect(figure?.querySelector(".deck-config-cards-heading")?.textContent).toBe("Example configurations");
      const footer = container.querySelector(".deck-config-card-footer");
      expect(footer?.getAttribute("data-split")).toBe("true");
      expect(footer?.querySelector(".deck-config-card-footer-label")?.textContent).toBe("Capacity");
      expect(footer?.querySelector(".deck-config-card-footer-value")?.textContent).toBe("Included allowance");
    }
    expect(stylesheet).toMatch(/\.deck-root \.deck-config-rows \{[^}]*grid-template-columns: minmax\(0, 1fr\) max-content;/);
    expect(stylesheet).toMatch(/\.deck-root \.deck-config-row \{[^}]*grid-template-columns: subgrid;/);
    expect(stylesheet).toMatch(/\[data-row-style="outlined"\] \.deck-config-row \{[^}]*border: 1px solid var\(--tone\);/);
    expect(stylesheet).toMatch(/\.deck-root \.deck-canvas \.deck-config-card-title \{\s*color: var\(--deck-fg\);/);
    expect(stylesheet).toMatch(/\.deck-config-cards\[data-cards="4"\] \.deck-config-card-title \{ font-size: var\(--deck-fs-sm\); \}/);
  });

  test("footer bands of one row stay level and a tinted chip ends with the solid chips", () => {
    const { container } = renderIn("light", <ConfigCardsBlockView block={block} />);
    // UX round 1 B1: every band stretches to the footer row its card shares, so a
    // label on two lines cannot make one band taller than its neighbours.
    expect(container.querySelector(".deck-config-card-footer")?.getAttribute("title")).toBe("Capacity");
    expect(stylesheet).toMatch(/\.deck-root \.deck-config-card-footer \{[^}]*align-self: stretch;/);
    expect(stylesheet).toMatch(/\.deck-root \.deck-config-card-footer-value \{[^}]*flex: none;[^}]*white-space: nowrap;/);
    // UX round 2 B1: the label is never cut (no ellipsis); it wraps as a last resort.
    const label = stylesheet.match(/\.deck-root \.deck-config-card-footer-label \{[^}]*\}/)?.[0] ?? "";
    expect(label).toContain("overflow-wrap: anywhere;");
    expect(label).not.toContain("text-overflow");
    expect(label).not.toContain("nowrap");
    // UX round 1 B2: the transparent chip of a tinted row ends at the row end, in line with the chips under it.
    expect(stylesheet).toMatch(
      /\.deck-root \.deck-config-row\[data-tinted="true"\] \.deck-config-chip \{[^}]*justify-self: end;[^}]*text-align: end;/,
    );
  });

  test("four cards step the footer band down so the reference footer holds one line", () => {
    // UX round 2 B1 on the reference content: a 354 px band, padding 24 + 24, gap 16 and
    // 20 px type left 125 px for a 204 px "Agentic capacity". Four cards step the band to
    // 16 px type, 16 px side padding and an 8 px gap: 163 + 8 + 135 = 306 of 322 px.
    const four: DeckConfigCardsBlock = {
      ...block,
      cards: [0, 1, 2, 3].map((index) => ({
        title: `Configuration ${index}`,
        rows: [{ label: "Module", chip: "Enterprise Edition" }],
        footer: "Agentic capacity",
        footer_value: index % 2 ? "+ acts packs" : "Included allowance",
      })),
    };
    const { container } = renderIn("light", <ConfigCardsBlockView block={four} />);
    expect(container.querySelector(".deck-config-cards")?.getAttribute("data-cards")).toBe("4");
    const labels = [...container.querySelectorAll(".deck-config-card-footer-label")].map((node) => node.textContent);
    expect(labels).toEqual(["Agentic capacity", "Agentic capacity", "Agentic capacity", "Agentic capacity"]);
    const rule = stylesheet.match(/\.deck-config-cards\[data-cards="4"\] \.deck-config-card-footer \{[^}]*\}/)?.[0] ?? "";
    expect(rule).toContain("gap: var(--deck-space-1);");
    expect(rule).toContain("padding-inline: var(--deck-space-2);");
    expect(rule).toContain("font-size: calc(var(--deck-fs-xs) * 0.8);");
  });

  test("plain rows and no heading keep the earlier look", () => {
    const { container } = renderIn("light", <ConfigCardsBlockView block={{ ...block, heading: undefined, row_style: "plain" }} />);
    expect(container.querySelector(".deck-config-cards")?.getAttribute("data-row-style")).toBe("plain");
    expect(container.querySelector(".deck-config-cards-heading")).toBeNull();
  });
});

describe("formula", () => {
  const block: DeckFormulaBlock = {
    type: "formula",
    terms: [
      { title: "Modules used", logos: [{ file_id: DARK_LOGO, logo_light_file_id: LIGHT_LOGO }, OTHER_LOGO] },
      { title: "One license", variant: "result" },
    ],
  };

  const shown = (container: HTMLElement) =>
    [...container.querySelectorAll(".deck-formula-logos img")].map((img) => img.getAttribute("src"));

  test("a row logo shows its light variant on a light slide", () => {
    const { container } = renderIn("light", <FormulaBlockView block={block} />);
    expect(shown(container)).toEqual([`data:image/svg+xml,${LIGHT_LOGO}`, `data:image/svg+xml,${OTHER_LOGO}`]);
  });

  test("a 4- or 5-term formula steps its titles down in a narrow box and wraps a long word inside its card", () => {
    const { container } = renderIn("dark", <FormulaBlockView block={block} />);
    // The figure is the container; data-terms sits on a DESCENDANT, the row, since a
    // container query never styles its own container (UX round 4 B1: gap stayed 24 px).
    const row = container.querySelector(".deck-formula > .deck-formula-row");
    expect(row?.getAttribute("data-terms")).toBe("2");
    expect(container.querySelector(".deck-formula")?.hasAttribute("data-terms")).toBe(false);
    expect(stylesheet).toMatch(/\.deck-root \.deck-formula \{ container-type: inline-size; \}/);
    expect(stylesheet).toMatch(/\.deck-formula-title \{ overflow-wrap: anywhere;/);
    const query = stylesheet.slice(stylesheet.indexOf("@container (max-width: 1200px) {\n  .deck-root .deck-formula-row"));
    expect(query).toMatch(/\.deck-formula-row:is\(\[data-terms="4"\], \[data-terms="5"\]\) \{ gap: var\(--deck-space-1\); \}/);
    expect(query).toMatch(
      /\.deck-formula-row:is\(\[data-terms="4"\], \[data-terms="5"\]\) \.deck-formula-term \{ padding: var\(--deck-space-1\); \}/,
    );
    expect(query).toMatch(
      /\.deck-formula-row:is\(\[data-terms="4"\], \[data-terms="5"\]\) \.deck-formula-title \{ font-size: var\(--deck-fs-sm\); \}/,
    );
    // No rule inside a container query may target the container itself.
    expect(stylesheet).not.toMatch(/\.deck-formula(:is\([^)]*\)|\[data-terms[^\]]*\]) \{/);
  });

  test("a logo term with show_title false draws its wordmark once, the title as its alt text", () => {
    for (const scheme of ["light", "dark"] as const) {
      const { container } = renderIn(
        scheme,
        <FormulaBlockView
          block={{
            type: "formula",
            terms: [
              { title: "Core", logo_file_id: DARK_LOGO, show_title: false, subtitle: "Included" },
              { title: "One license", variant: "result" },
            ],
          }}
        />,
      );
      const terms = container.querySelectorAll(".deck-formula-term");
      expect(terms[0].querySelector(".deck-formula-title")).toBeNull();
      expect(terms[0].querySelector(".deck-formula-subtitle")?.textContent).toBe("Included");
      expect(terms[1].querySelector(".deck-formula-title")?.textContent).toBe("One license");
    }
  });

  test("terms centre by default, keep a logo row on one line and wear their own tone on the top bar", () => {
    const { container } = renderIn(
      "light",
      <FormulaBlockView block={{ ...block, terms: [{ ...block.terms[0], tone: "brand_2" }, block.terms[1]] }} />,
    );
    expect(container.querySelector(".deck-formula-row")?.getAttribute("data-align")).toBe("center");
    expect(container.querySelector(".deck-formula-term")?.getAttribute("data-color")).toBe("brand_2");
    expect(stylesheet).toMatch(/\.deck-formula-row\[data-align="center"\] \.deck-formula-term \{\s*align-items: center;/);
    expect(stylesheet).toMatch(/\.deck-root \.deck-formula-logos \{[^}]*flex-wrap: nowrap;/);
    expect(stylesheet).toMatch(/\.deck-root \.deck-formula-term \{[^}]*border-top: 4px solid var\(--tone\);/);
    const start = renderIn("light", <FormulaBlockView block={{ ...block, align: "start" }} />);
    expect(start.container.querySelector(".deck-formula-row")?.getAttribute("data-align")).toBe("start");
  });

  test("a row logo keeps its own file on a dark slide", () => {
    const { container } = renderIn("dark", <FormulaBlockView block={block} />);
    expect(shown(container)).toEqual([`data:image/svg+xml,${DARK_LOGO}`, `data:image/svg+xml,${OTHER_LOGO}`]);
  });
});
