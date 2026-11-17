import { describe, expect, it } from "vitest";
import { designRootAttributes, templateDesignOf } from "./design-tokens";

describe("designRootAttributes", () => {
  it("publishes nothing for a template that declares no design", () => {
    expect(designRootAttributes(null)).toEqual({ style: {} });
    expect(designRootAttributes(undefined)).toEqual({ style: {} });
  });

  it("publishes the flat switches and the radius and spacing variables", () => {
    const attrs = designRootAttributes({
      shadows: false,
      glow: false,
      eyebrows: false,
      gradient_strokes: "never",
      radius: { card: 12, inner: 8, chip: 4, table: 0 },
      spacing: { margin_x: 96, margin_top: 72, footer: 56, tile_gap: 24 },
      body_align: "top",
    });
    expect(attrs["data-design"]).toBe("true");
    expect(attrs["data-design-shadows"]).toBe("false");
    expect(attrs["data-design-glow"]).toBe("false");
    expect(attrs["data-design-eyebrows"]).toBe("false");
    expect(attrs["data-design-gradient-strokes"]).toBe("never");
    expect(attrs["data-design-body-align"]).toBe("top");
    expect(attrs.style).toEqual({
      "--deck-radius": "12px",
      "--deck-radius-sm": "8px",
      "--deck-radius-chip": "4px",
      "--deck-radius-table": "0px",
      "--deck-pad-x": "96px",
      "--deck-pad-top": "72px",
      "--deck-pad-bottom": "56px",
      "--deck-tile-gap": "24px",
    });
  });

  it("keeps today's switches when the template leaves them on", () => {
    const attrs = designRootAttributes({ shadows: true, radius: { card: 16 } });
    expect(attrs["data-design-shadows"]).toBeUndefined();
    expect(attrs["data-design-glow"]).toBeUndefined();
    expect(attrs.style).toEqual({ "--deck-radius": "16px" });
  });
});

describe("templateDesignOf", () => {
  it("reads a template row the way the deck response serves it", () => {
    expect(templateDesignOf({ extra_metadata: {} })).toBeNull();
    expect(
      templateDesignOf({
        extra_metadata: {
          design: { glow: false, gradient_strokes: "maybe", radius: { card: 12, chip: -2 }, spacing: { bogus: 1 } },
        },
      }),
    ).toEqual({ glow: false, radius: { card: 12 } });
  });
});
