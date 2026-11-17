/**
 * The template's brand mark placed IN A BLOCK reads like the chrome reads it
 * (issue #11579) - `yarn test:dom`.
 *
 * A template that inks its mark (`chrome.emblem_ink: "accent"`) drew it in
 * the chrome as a mask over `--deck-emblem-ink` (the accent on a light slide,
 * the foreground on a dark one), but the same file named by an architecture
 * node, a zone, an image or a row logo kept its own white fill: a white symbol
 * on a pale light slide. Every asset is a generic placeholder.
 */
import { readFileSync } from "node:fs";

import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckArchitectureBlock, PresentationThemeConfig, PresentationThemeScheme } from "@/types";

import { DeckAssetMetaProvider, DeckAssetProvider } from "./asset-context";
import { ArchitectureBlockView } from "./blocks/Architecture";
import { RowLogo } from "./blocks/shared";
import { brandPicture, DeckBrandContext, deckBrandAssets, type DeckBrandAssets } from "./brand-picture";
import { SlideSchemeContext } from "./theme";

vi.mock("./deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

const MARK = "00000000-0000-4000-8000-00000000c001";
const MARK_LIGHT = "00000000-0000-4000-8000-00000000c002";
const LOGO = "00000000-0000-4000-8000-00000000c003";
const LOGO_LIGHT = "00000000-0000-4000-8000-00000000c004";
const OTHER = "00000000-0000-4000-8000-00000000c005";

const SRC = "data:image/svg+xml;base64,PHN2Zy8+";

function brand(overrides: Partial<DeckBrandAssets> = {}): DeckBrandAssets {
  return { marks: { dark: MARK, light: MARK }, logos: [LOGO], logoLight: LOGO_LIGHT, inkMark: true, ...overrides };
}

describe("brandPicture", () => {
  test("the template's mark on an inking template is inked on both schemes", () => {
    expect(brandPicture(MARK, brand(), "light")).toEqual({ fileId: MARK, inked: true });
    expect(brandPicture(MARK, brand(), "dark")).toEqual({ fileId: MARK, inked: true });
  });

  test("the mark keeps its own colours on a template that does not ink it", () => {
    expect(brandPicture(MARK, brand({ inkMark: false }), "light")).toEqual({ fileId: MARK, inked: false });
  });

  test("the mark of the other scheme is swapped for the slide's own", () => {
    const own = brand({ marks: { dark: MARK, light: MARK_LIGHT } });
    expect(brandPicture(MARK, own, "light").fileId).toBe(MARK_LIGHT);
    expect(brandPicture(MARK_LIGHT, own, "dark").fileId).toBe(MARK);
  });

  test("the template's logo takes its light variant on a light slide only", () => {
    expect(brandPicture(LOGO, brand(), "light")).toEqual({ fileId: LOGO_LIGHT, inked: false });
    expect(brandPicture(LOGO, brand(), "dark")).toEqual({ fileId: LOGO, inked: false });
  });

  test("any other file, or no slide root, is drawn as it is", () => {
    expect(brandPicture(OTHER, brand(), "light")).toEqual({ fileId: OTHER, inked: false });
    expect(brandPicture(MARK, null, "light")).toEqual({ fileId: MARK, inked: false });
    expect(brandPicture(MARK, brand(), undefined)).toEqual({ fileId: MARK, inked: false });
  });

  test("deckBrandAssets reads the palettes of both schemes and the chrome's ink", () => {
    const light = { mark_file_id: MARK, logo_file_id: LOGO, logo_light_file_id: LOGO_LIGHT } as PresentationThemeConfig;
    const dark = { mark_file_id: MARK, logo_file_id: LOGO } as PresentationThemeConfig;
    const assets = deckBrandAssets({
      theme_config: dark,
      theme_configs: { dark, light },
      template_chrome: { emblem_ink: "accent" },
    } as Parameters<typeof deckBrandAssets>[0]);
    expect(assets).toEqual({ marks: { dark: MARK, light: MARK }, logos: [LOGO], logoLight: LOGO_LIGHT, inkMark: true });
    expect(deckBrandAssets({ theme_config: dark, template_chrome: null } as Parameters<typeof deckBrandAssets>[0]).inkMark).toBe(false);
  });
});

function onSlide(scheme: PresentationThemeScheme, assets: DeckBrandAssets | null, children: React.ReactNode) {
  return renderWithProviders(
    <div className="deck-root" data-scheme={scheme}>
      <SlideSchemeContext.Provider value={{ scheme, overridden: false, tokens: {} as PresentationThemeConfig }}>
        <DeckBrandContext.Provider value={assets}>
          <DeckAssetProvider value={() => SRC}>
            <DeckAssetMetaProvider value={{}}>{children}</DeckAssetMetaProvider>
          </DeckAssetProvider>
        </DeckBrandContext.Provider>
      </SlideSchemeContext.Provider>
    </div>,
  );
}

const NODE_BLOCK: DeckArchitectureBlock = {
  type: "architecture",
  nodes: [
    { id: "core", label: "Core", logo_file_id: MARK, row: 1, col: 1 },
    { id: "other", label: "Other", logo_file_id: OTHER, row: 1, col: 2 },
  ],
  edges: [],
};

describe("an architecture node showing the template's mark", () => {
  test("is drawn as a mask over the emblem ink, the image kept transparent for the ready signal", () => {
    const { container } = onSlide("light", brand(), <ArchitectureBlockView block={NODE_BLOCK} />);
    const inks = container.querySelectorAll("rect.deck-arch-logo-ink[data-brand-ink='true']");
    expect(inks).toHaveLength(1);
    const mask = inks[0].getAttribute("mask");
    expect(mask).toMatch(/^url\(#deck-arch-logo-mask-/);
    expect(container.querySelector(`mask${mask!.slice(4, -1)} image`)).not.toBeNull();
    const images = Array.from(container.querySelectorAll("image.deck-arch-logo"));
    expect(images).toHaveLength(2);
    expect(images.filter((image) => image.getAttribute("opacity") === "0")).toHaveLength(1);
  });

  test("bounds the mask region to the ink box, so no ink paints past the mark", () => {
    const { container } = onSlide("light", brand(), <ArchitectureBlockView block={NODE_BLOCK} />);
    const ink = container.querySelector("rect.deck-arch-logo-ink")!;
    const mask = container.querySelector(`mask${ink.getAttribute("mask")!.slice(4, -1)}`)!;
    for (const attr of ["x", "y", "width", "height"]) {
      expect(mask.getAttribute(attr)).not.toBeNull();
      expect(mask.getAttribute(attr)).toBe(ink.getAttribute(attr));
    }
  });

  test("keeps its own colours on a template that does not ink its mark", () => {
    const { container } = onSlide("light", brand({ inkMark: false }), <ArchitectureBlockView block={NODE_BLOCK} />);
    expect(container.querySelector("rect.deck-arch-logo-ink")).toBeNull();
  });
});

describe("a row logo showing the template's mark", () => {
  test("carries the brand ink and the mask of its own bytes", () => {
    const { container } = onSlide("light", brand(), <RowLogo fileId={MARK} />);
    const img = container.querySelector("img.deck-row-logo-img");
    expect(img?.getAttribute("data-brand-ink")).toBe("true");
    expect((img as HTMLElement).style.getPropertyValue("--deck-brand-mask")).toContain(SRC);
  });

  test("another picture is left as it is", () => {
    const { container } = onSlide("light", brand(), <RowLogo fileId={OTHER} />);
    expect(container.querySelector("img.deck-row-logo-img")?.hasAttribute("data-brand-ink")).toBe(false);
  });
});

describe("the brand ink rules", () => {
  const stylesheet = readFileSync(`${__dirname}/presentation.css`, "utf8");
  const rule = (selector: string) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(stylesheet);
    expect(match, `no rule for ${selector}`).not.toBeNull();
    return match![1];
  };

  test("paint the emblem ink through a mask, never a filter", () => {
    const img = rule('.deck-root img[data-brand-ink="true"]');
    expect(img).toMatch(/background-color:\s*var\(--deck-emblem-ink\)/);
    expect(img).toMatch(/(?:^|\s)mask:\s*var\(--deck-brand-mask\)/);
    expect(img).not.toMatch(/filter:/);
    expect(rule(".deck-root .deck-arch-logo-ink")).toMatch(/fill:\s*var\(--deck-emblem-ink\)/);
  });
});
