import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import type { PresentationSlide } from "@/types";

import { plainAgendaColumns, resolveAgendaLayout } from "./agenda-layout";
import { designRootAttributes, templateDesignOf } from "./design-tokens";
import { slideShowsFooter } from "./layouts/footer";
import { resolveLogoPosition } from "./logo-position";

type AgendaArg = Parameters<typeof resolveAgendaLayout>[0];

function agenda(count: number, extra: Partial<AgendaArg> = {}): AgendaArg {
  return {
    layout: "agenda",
    items: Array.from({ length: count }, (_, index) => ({ title: `Item ${index + 1}` })),
    ...extra,
  } as AgendaArg;
}

describe("slide compositions", () => {
  it("publishes the compositions on the slide root", () => {
    const attrs = designRootAttributes({
      title_underline: false,
      title_ink: "brand",
      cover: "brand",
      section: "outline",
      agenda: "plain",
    });
    expect(attrs["data-design-title-underline"]).toBe("false");
    expect(attrs["data-design-title-ink"]).toBe("brand");
    expect(attrs["data-design-cover"]).toBe("brand");
    expect(attrs["data-design-section"]).toBe("outline");
    expect(attrs["data-design-agenda"]).toBe("plain");
  });

  it("leaves a template without compositions as today", () => {
    const attrs = designRootAttributes({ shadows: false });
    expect(attrs["data-design-cover"]).toBeUndefined();
    expect(attrs["data-design-section"]).toBeUndefined();
    expect(attrs["data-design-agenda"]).toBeUndefined();
    expect(attrs["data-design-title-underline"]).toBeUndefined();
  });

  it("reads only the known composition values from extra_metadata", () => {
    const design = templateDesignOf({
      extra_metadata: { design: { cover: "brand", section: "ghost", agenda: "cards", title_underline: false } },
    });
    expect(design?.cover).toBe("brand");
    expect(design?.section).toBeUndefined();
    expect(design?.agenda).toBeUndefined();
    expect(design?.title_underline).toBe(false);
  });

  it("lays the plain agenda in two columns, three from seven items", () => {
    expect(plainAgendaColumns(3)).toBe(2);
    expect(plainAgendaColumns(6)).toBe(2);
    expect(plainAgendaColumns(7)).toBe(3);
    const layout = resolveAgendaLayout(agenda(4), "plain");
    expect(layout.columns).toBe(2);
    expect(layout.style).toBe("list");
    // Absent `distribute` reads as the API default: the rows spread.
    expect(layout.distribute).toBe(true);
    expect(resolveAgendaLayout(agenda(8), "plain").columns).toBe(3);
  });

  it("spreads the agenda rows with distribute true and packs them with distribute false", () => {
    // The API stores `distribute: true` on every agenda (AgendaSlide default):
    // the rows spread over the body height, on the plain (brand) agenda too.
    expect(resolveAgendaLayout(agenda(4, { distribute: true }), "plain").distribute).toBe(true);
    expect(resolveAgendaLayout(agenda(8, { distribute: true }), "plain").distribute).toBe(true);
    expect(resolveAgendaLayout(agenda(4, { distribute: true })).distribute).toBe(true);
    // `distribute: false` packs them on top, plain or not.
    expect(resolveAgendaLayout(agenda(4, { distribute: false }), "plain").distribute).toBe(false);
    expect(resolveAgendaLayout(agenda(8, { distribute: false }), "plain").distribute).toBe(false);
    expect(resolveAgendaLayout(agenda(4, { distribute: false })).distribute).toBe(false);
  });

  it("spreads 2 to 5 agenda rows, list and grid, with the stored distribute", () => {
    // The stored shape: `distribute: true`. The row count is what the bounded
    // spread caps (`spread.test.ts`): 2 and 3 rows centred, 4 and 5 filled.
    const cases: [number, 1 | 2 | 3, number][] = [
      [4, 2, 2],
      [8, 3, 3],
      [4, 1, 4],
      [10, 2, 5],
    ];
    for (const style of ["list", "grid"] as const) {
      for (const [count, columns, rows] of cases) {
        const layout = resolveAgendaLayout(agenda(count, { distribute: true, columns, style }));
        expect(layout.distribute).toBe(true);
        expect(layout.rows).toHaveLength(rows);
      }
    }
    expect(resolveAgendaLayout(agenda(4, { distribute: true, columns: 2 }), "plain").rows).toHaveLength(2);
    expect(resolveAgendaLayout(agenda(8, { distribute: true, columns: 3 }), "plain").rows).toHaveLength(3);
  });

  it("centres a capped distribute agenda as a group", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    const spread = css.match(/\.deck-agenda\[data-distribute="true"\] \{([^}]*)\}/);
    expect(spread?.[1]).toMatch(/justify-content:\s*space-between/);
    expect(spread?.[1]).toMatch(/margin-block:\s*auto/);
  });

  it("centres the agenda chip and number on the text block, not the title line", () => {
    // The geometry (chip centre within 2 px of the text block centre, 1-line
    // and 2-line items, stored `distribute: true`) is measured on a live deck
    // by `tests_e2e/presentation-agenda-centre.spec.ts`; the rules behind it:
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    const rule = (selector: string) => css.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} \\{([^}]*)\\}`))?.[1];
    // The list chip spans the title and description tracks and is centred on them.
    const marker = rule('.deck-agenda[data-style="list"] .deck-agenda-marker');
    expect(marker).toMatch(/grid-row:\s*1 \/ span 2/);
    expect(marker).toMatch(/align-self:\s*center/);
    // An item without a description: the chip on its title alone.
    expect(rule('.deck-agenda[data-style="list"] .deck-agenda-item[data-description="none"] .deck-agenda-marker')).toMatch(
      /grid-row:\s*1;/,
    );
    // ... held inside the shared title track when the slide reserves the line.
    expect(
      rule(
        '.deck-agenda[data-style="list"][data-descriptions="true"] .deck-agenda-item[data-description="none"] .deck-agenda-marker',
      ),
    ).toMatch(/margin-block:\s*-32px/);
    expect(rule('.deck-agenda[data-style="list"]:not([data-descriptions="true"]) .deck-agenda-title')).toMatch(
      /align-self:\s*center/,
    );
    // The grid's `N.` is centred on its title.
    expect(rule(".deck-root .deck-agenda-head")).toMatch(/align-items:\s*center/);
    // The chip is 64px: the hold is half of it on each side.
    expect(rule(".deck-root .deck-agenda-marker")).toMatch(/height:\s*64px/);
  });

  it("anchors the brand closing logo to the bottom, the title group centred", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    const logo = css.match(/\.deck-closing\[data-brand="true"\] \.deck-cover-logo \{([^}]*)\}/);
    expect(logo?.[1]).toMatch(/margin-top:\s*auto/);
    const main = css.match(/\.deck-closing\[data-brand="true"\] \.deck-closing-main \{([^}]*)\}/);
    expect(main?.[1]).toMatch(/margin-top:\s*auto/);
    // The plain agenda packs on top only when the slide does not distribute.
    const packed = css.match(/\.deck-agenda\[data-plain="true"\]:not\(\[data-distribute="true"\]\) \{([^}]*)\}/);
    expect(packed?.[1]).toMatch(/justify-content:\s*flex-start/);
    expect(css).not.toMatch(/\.deck-agenda\[data-plain="true"\] \{[^}]*justify-content:\s*flex-start/);
  });

  it("draws the brand background emblem large, cropped off the bottom right", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    // The backdrop clips: the emblem bleeds off the slide edge, never past the page.
    const backdrop = css.match(/\.deck-root \.deck-brand-backdrop \{([^}]*)\}/);
    expect(backdrop?.[1]).toMatch(/overflow:\s*hidden/);
    const emblem = css.match(/\.deck-brand-backdrop \.deck-brand-emblem \{([^}]*)\}/)?.[1] ?? "";
    // Sized on the slide height (95%) so 16:9 and 4:3 crop alike, anchored on
    // the bottom-right corner and pushed 22% of its size past both edges.
    // A template's `cover_emblem` sets the size and opacity through
    // variables; the fallbacks are the values checked here.
    expect(emblem).toMatch(/height:\s*var\(--deck-cover-emblem-scale,\s*95%\)/);
    expect(emblem).toMatch(/right:\s*0;/);
    expect(emblem).toMatch(/bottom:\s*0;/);
    expect(emblem).toMatch(/transform:\s*translate\(22%,\s*22%\)/);
    // Opacity and ink unchanged.
    expect(emblem).toMatch(/opacity:\s*var\(--deck-cover-emblem-opacity,\s*0\.07\)/);
    expect(emblem).toMatch(/filter:\s*brightness\(0\) invert\(1\)/);
  });

  it("publishes a declared cover emblem on the slide root", () => {
    const attrs = designRootAttributes({ cover: "brand", cover_emblem: { scale: 120, anchor: "right", opacity: 5 } });
    const style = attrs.style as Record<string, string>;
    expect(style["--deck-cover-emblem-scale"]).toBe("120%");
    expect(style["--deck-cover-emblem-opacity"]).toBe("0.05");
    expect(attrs["data-design-cover-emblem-anchor"]).toBe("right");
    // The corner crop is the stylesheet's default: no attribute for it.
    const corner = designRootAttributes({ cover: "brand", cover_emblem: { anchor: "bottom_right" } });
    expect(corner["data-design-cover-emblem-anchor"]).toBeUndefined();
  });

  it("leaves a template without a cover emblem on the stylesheet's values", () => {
    const attrs = designRootAttributes({ cover: "brand" });
    const style = attrs.style as Record<string, string>;
    expect(style["--deck-cover-emblem-scale"]).toBeUndefined();
    expect(style["--deck-cover-emblem-opacity"]).toBeUndefined();
    expect(attrs["data-design-cover-emblem-anchor"]).toBeUndefined();
  });

  it("reads only a cover emblem in range from extra_metadata", () => {
    const design = templateDesignOf({
      extra_metadata: { design: { cover: "brand", cover_emblem: { scale: 300, anchor: "top_left", opacity: 7 } } },
    });
    expect(design?.cover_emblem).toEqual({ opacity: 7 });
    const none = templateDesignOf({ extra_metadata: { design: { cover: "brand", cover_emblem: { opacity: 40 } } } });
    expect(none?.cover_emblem).toBeUndefined();
  });

  it("draws the inside-the-edge cover emblem whole", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    const right = css.match(
      /\[data-design-cover-emblem-anchor="right"\] \.deck-brand-backdrop \.deck-brand-emblem \{([^}]*)\}/,
    )?.[1] ?? "";
    // A scale above 100% never crops the mark under this anchor.
    expect(right).toMatch(/height:\s*min\(var\(--deck-cover-emblem-scale,\s*95%\),\s*90%\)/);
    expect(right).toMatch(/top:\s*50%/);
    expect(right).toMatch(/bottom:\s*auto/);
    expect(right).toMatch(/transform:\s*translateY\(-50%\)/);
  });

  it("honours the cover's align under the brand cover", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    // The brand cover centres only under `align: center`; its bare rule never
    // centres the text, so `align: start` draws the left layout.
    const bare = css.match(/\.deck-title\[data-brand="true"\] \{([^}]*)\}/);
    expect(bare?.[1]).not.toMatch(/align-items:\s*center/);
    expect(bare?.[1]).not.toMatch(/text-align:\s*center/);
    const logo = css.match(/\.deck-title\[data-brand="true"\] \.deck-cover-logo \{([^}]*)\}/);
    expect(logo?.[1]).toMatch(/justify-content:\s*flex-start/);
    const centred = css.match(/\.deck-title\[data-brand="true"\]\[data-align="center"\] \{([^}]*)\}/);
    expect(centred?.[1]).toMatch(/align-items:\s*center/);
    expect(centred?.[1]).toMatch(/text-align:\s*center/);
    expect(css).toMatch(/\.deck-title\[data-brand="true"\]\[data-align="center"\] \.deck-cover-logo \{\s*justify-content:\s*center;/);
  });

  it("draws the closing on the left with align start, centred otherwise", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    const start = css.match(/\.deck-closing\[data-align="start"\] \{([^}]*)\}/);
    expect(start?.[1]).toMatch(/align-items:\s*flex-start/);
    expect(start?.[1]).toMatch(/text-align:\s*left/);
    const closing = (align?: "start" | "center") =>
      ({ layout: "closing", title: "Thank you", align }) as unknown as PresentationSlide;
    expect(resolveLogoPosition("auto", closing("start"))).toBe("start");
    expect(resolveLogoPosition("auto", closing("center"))).toBe("center");
    expect(resolveLogoPosition(undefined, closing())).toBe("center");
  });

  it("draws every title of a brand-ink deck in one ink on light, the section title included", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    const at = css.indexOf('.deck-root [data-scheme="light"][data-design-title-ink="brand"] .deck-slide-header h2');
    expect(at).toBeGreaterThanOrEqual(0);
    const block = css.slice(at, css.indexOf("}", at));
    const [selectors, body] = block.split("{");
    expect(selectors).toContain('[data-scheme="light"][data-design-title-ink="brand"] .deck-section h1');
    expect(body).toMatch(/color:\s*var\(--deck-accent\)/);
  });

  it("draws the gradient bar under a brand-ink body title unless the template turns it off", () => {
    const css = readFileSync(`${__dirname}/presentation.css`, "utf8");
    const on = '.deck-root [data-design-title-ink="brand"]:not([data-design-title-underline="false"])';
    const rule = (selector: string) => {
      const at = css.indexOf(`${selector} {`);
      expect(at, selector).toBeGreaterThanOrEqual(0);
      return css.slice(at + selector.length, css.indexOf("}", at));
    };
    // The bar: the 96 x 4 px rule in the template gradient, 12 px under the title.
    const bar = rule(`${on} .deck-slide-header:has(> h2)::after`);
    expect(bar).toMatch(/content:\s*""/);
    expect(bar).toMatch(/width:\s*96px/);
    expect(bar).toMatch(/height:\s*4px/);
    expect(bar).toMatch(/margin-top:\s*12px/);
    expect(bar).toMatch(/linear-gradient\(90deg, var\(--deck-gradient-from\), var\(--deck-gradient-to\)\)/);
    // Between the title and the subtitle.
    expect(bar).toMatch(/order:\s*1/);
    expect(rule(`${on} .deck-slide-header:has(> h2) > :not(h2)`)).toMatch(/order:\s*2/);
    // The body top does not move: the bar's 16 px come off the gap under the header.
    expect(rule(`${on} .deck-slide-header:has(> h2)`)).toMatch(
      /margin-bottom:\s*calc\(var\(--deck-title-body-gap, var\(--deck-space-5\)\) - var\(--deck-space-2\)\)/,
    );
    expect(rule(`${on} .deck-slide[data-layout="dashboard"] .deck-slide-header:has(> h2)`)).toMatch(
      /margin-bottom:\s*calc\(var\(--deck-space-3\) - var\(--deck-space-2\)\)/,
    );
    // The clamped title never draws its own bar (its overflow would clip it).
    expect(rule('.deck-root [data-design-title-ink="brand"] .deck-slide-header h2::after')).toMatch(/content:\s*none/);
    // A template without the brand ink keeps the default title bar.
    expect(rule(".deck-root .deck-slide-header h2::after")).toMatch(/width:\s*96px/);
    // The cover, section and closing draw no slide header, so no bar.
    expect(css).not.toMatch(/\.deck-(title|section|closing)[^{]*\.deck-slide-header:has\(> h2\)::after/);
  });

  it("publishes the title bar switch only when the template turns it off", () => {
    expect(designRootAttributes({ title_ink: "brand", title_underline: true })["data-design-title-underline"]).toBeUndefined();
    expect(designRootAttributes({ title_ink: "brand" })["data-design-title-underline"]).toBeUndefined();
    expect(designRootAttributes({ title_ink: "brand", title_underline: false })["data-design-title-underline"]).toBe("false");
  });

  it("gives the section slide the body footer only when the template asks", () => {
    const section = { layout: "section", title: "Where we were" } as unknown as PresentationSlide;
    expect(slideShowsFooter(section)).toBe(false);
    expect(slideShowsFooter(section, true)).toBe(true);
    const title = { layout: "title", title: "Cover" } as unknown as PresentationSlide;
    expect(slideShowsFooter(title, true)).toBe(false);
  });
});
