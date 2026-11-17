/**
 * Unit tests for the hero / overlay catalog readers - `yarn test:unit`.
 *
 * The layouts, the slide roots and the editor all read a slide's look through
 * these; what is pinned is the default each reads when the field is absent or
 * carries a value outside the catalog, and which layouts have a hero at all.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { PresentationSlide } from "@/types";

import {
  HERO_LAYOUTS,
  HERO_TONES,
  HERO_VARIANTS,
  MOSAIC_HEROES,
  OVERLAY_VARIANTS,
  SHARED_HEROES,
  TEMPLATE_HEROES,
  coverHero,
  heroHostsBackgroundImage,
  heroHostsLogo,
  slideHero,
  slideHeroTone,
  slideOverlay,
  templateHeroDefaults,
  templateHeroes,
} from "./hero.ts";

const slide = (fields: Record<string, unknown>): PresentationSlide =>
  ({ title: "T", ...fields }) as unknown as PresentationSlide;

test("the catalogs mirror the backend's, in order", () => {
  assert.deepEqual(
    [...HERO_VARIANTS],
    ["plain", "gradient", "split", "spotlight", "mosaic", "cascade", "horizon", "pillar"],
  );
  assert.deepEqual(
    [...HERO_TONES],
    ["brand", "accent", "gradient_from", "gradient_to", "info", "success", "warning", "error", "muted"],
  );
  assert.deepEqual([...OVERLAY_VARIANTS], ["none", "soft", "strong"]);
  assert.deepEqual([...HERO_LAYOUTS], ["title", "closing"]);
});

test("a hero exists on the title and closing slides only, plain when absent or unknown", () => {
  assert.equal(slideHero(slide({ layout: "title" })), "plain");
  assert.equal(slideHero(slide({ layout: "closing", hero: "spotlight" })), "spotlight");
  assert.equal(slideHero(slide({ layout: "title", hero: "mosaic" })), "mosaic");
  assert.equal(slideHero(slide({ layout: "title", hero: "neon" })), "plain");
  assert.equal(slideHero(slide({ layout: "content", hero: "gradient" })), null);
  assert.equal(slideHero(slide({ layout: "section" })), null);
});

test("the hero colour and the overlay read their defaults when absent or unknown", () => {
  assert.equal(slideHeroTone(slide({ layout: "title" })), "brand");
  assert.equal(slideHeroTone(slide({ layout: "title", hero_tone: "accent" })), "accent");
  assert.equal(slideHeroTone(slide({ layout: "title", hero_tone: "#0fbcff" })), "brand");
  assert.equal(slideOverlay(slide({ layout: "content" })), "none");
  assert.equal(slideOverlay(slide({ layout: "content", overlay: "strong" })), "strong");
  assert.equal(slideOverlay(slide({ layout: "content", overlay: "heavy" })), "none");
});

test("only the split hero hosts the slide's background image in its own panel", () => {
  assert.equal(heroHostsBackgroundImage(slide({ layout: "title", hero: "split" })), true);
  assert.equal(heroHostsBackgroundImage(slide({ layout: "closing", hero: "split" })), true);
  assert.equal(heroHostsBackgroundImage(slide({ layout: "title", hero: "gradient" })), false);
  assert.equal(heroHostsBackgroundImage(slide({ layout: "title" })), false);
  // A body slide never hosts one: it has no hero.
  assert.equal(heroHostsBackgroundImage(slide({ layout: "content", hero: "split" })), false);
});

test("the tiled heroes keep the mosaic's own pattern, and only cascade and horizon draw the logo", () => {
  // `mosaic` is the original scatter; the shapes are new patterns of the same drawing.
  assert.deepEqual(MOSAIC_HEROES, { mosaic: "scatter", cascade: "cascade", horizon: "horizon", pillar: "pillar" });
  assert.equal(heroHostsLogo(slide({ layout: "title", hero: "cascade" })), true);
  assert.equal(heroHostsLogo(slide({ layout: "closing", hero: "horizon" })), true);
  for (const hero of ["plain", "gradient", "split", "spotlight", "mosaic", "pillar"]) {
    assert.equal(heroHostsLogo(slide({ layout: "title", hero })), false, hero);
  }
  // The chrome keeps the logo on every body slide, whatever a stray field says.
  assert.equal(heroHostsLogo(slide({ layout: "content", hero: "cascade" })), false);
});

test("the tiled covers are a template's own: only a template that declares them offers them", () => {
  assert.deepEqual([...SHARED_HEROES], ["plain", "gradient", "split", "spotlight"]);
  assert.deepEqual([...TEMPLATE_HEROES], ["mosaic", "cascade", "horizon", "pillar"]);
  // A brand template that declares nothing, a free deck: the shared covers.
  assert.deepEqual(templateHeroes({ deck_defaults: { hero_defaults: { title: "gradient" } } }), [...SHARED_HEROES]);
  assert.deepEqual(templateHeroes(undefined), [...SHARED_HEROES]);
  assert.deepEqual(templateHeroes({ heroes: "mosaic" }), [...SHARED_HEROES]);
  // The THREAD way: its own covers on top, catalog order, unknown names ignored.
  assert.deepEqual(templateHeroes({ heroes: ["Pillar", "mosaic", "neon", "gradient"] }), [
    ...SHARED_HEROES,
    "mosaic",
    "pillar",
  ]);
});

test("a cover default is read among the template's covers, and a cover it does not offer takes it or gradient", () => {
  const brand = { deck_defaults: { hero_defaults: { title: "Split", closing: "cascade", content: "gradient" } } };
  assert.deepEqual(templateHeroDefaults(brand), { title: "split" });
  const thread = { heroes: ["cascade"], deck_defaults: { hero_defaults: { title: "cascade", closing: "cascade" } } };
  assert.deepEqual(templateHeroDefaults(thread), { title: "cascade", closing: "cascade" });
  assert.deepEqual(templateHeroDefaults({ deck_defaults: { hero_defaults: "gradient" } }), {});
  assert.equal(coverHero("title", SHARED_HEROES, templateHeroDefaults(brand)), "split");
  assert.equal(coverHero("closing", SHARED_HEROES, templateHeroDefaults(brand)), "gradient");
  assert.equal(coverHero("title", SHARED_HEROES, { title: "cascade" }), "gradient");
});
