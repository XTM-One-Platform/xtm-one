/**
 * "Preview the unsaved design" previews the template saving would write - `yarn test:unit`.
 *
 * Every field the preview reads comes from the form (name, aspect ratio,
 * layouts, palettes, deck defaults); a draft value a save would be refused for
 * shows the saved one; the saved template's own Preview is left alone.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import type { TemplateContainerDetail } from "@/types";

import {
  draftPreviewTemplate,
  MAX_TEMPLATE_ASPECT_RATIO_CHARS,
  MAX_TEMPLATE_NAME_CHARS,
  type TemplateDraft,
} from "./draft-template.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");

const saved = {
  id: "t1",
  name: "Saved brand",
  kind: "presentation",
  aspect_ratio: "16:9",
  layouts: [{ id: "title" }, { id: "content" }],
  theme_config: { background: "#070d18", accent: "#42caff" },
  extra_metadata: { supported_schemes: ["dark"] },
  supported_schemes: ["dark"],
  updated_at: "2026-09-27T10:00:00Z",
} as unknown as TemplateContainerDetail;

const unchanged: TemplateDraft = {
  name: saved.name,
  aspectRatio: "16:9",
  layouts: saved.layouts,
  themeConfig: saved.theme_config,
  extraMetadata: saved.extra_metadata,
};

test("every field the preview reads comes from the form", () => {
  const draft: TemplateDraft = {
    name: "Draft brand",
    aspectRatio: " 4:3 ",
    layouts: [{ id: "title" }, { id: "closing" }],
    themeConfig: { background: "#ffffff", accent: "#0015a8", chart_1: "#0015a8" },
    extraMetadata: { supported_schemes: ["light"], deck_defaults: { aspect_ratio: "A4", transition: "fade" } },
  };
  const preview = draftPreviewTemplate(saved, draft);
  assert.equal(preview.name, "Draft brand");
  assert.equal(preview.aspect_ratio, "4:3", "trimmed, as the form sends it");
  assert.deepEqual(preview.layouts, [{ id: "title" }, { id: "closing" }]);
  assert.deepEqual(preview.theme_config, draft.themeConfig);
  assert.deepEqual(preview.extra_metadata, draft.extraMetadata);
  // What the form does not edit is the saved template's.
  assert.equal(preview.id, saved.id);
  assert.equal(preview.updated_at, saved.updated_at);
  assert.equal(saved.name, "Saved brand", "the saved template is left as it was");
});

test("a blank aspect ratio clears it, as saving does", () => {
  assert.equal(draftPreviewTemplate(saved, { ...unchanged, aspectRatio: "  " }).aspect_ratio, "");
});

test("a draft value a save would be refused for shows the saved one", () => {
  for (const name of ["", "   ", "x".repeat(MAX_TEMPLATE_NAME_CHARS + 1)]) {
    assert.equal(draftPreviewTemplate(saved, { ...unchanged, name }).name, saved.name);
  }
  assert.equal(draftPreviewTemplate(saved, { ...unchanged, name: "x".repeat(MAX_TEMPLATE_NAME_CHARS) }).name.length, MAX_TEMPLATE_NAME_CHARS);
  assert.equal(
    draftPreviewTemplate(saved, { ...unchanged, aspectRatio: "9".repeat(MAX_TEMPLATE_ASPECT_RATIO_CHARS + 1) }).aspect_ratio,
    saved.aspect_ratio,
  );
  // Layouts JSON that does not parse (null), or is not a list of objects.
  for (const layouts of [null, {}, "title", ["title"], [{ id: "title" }, null], [[{ id: "title" }]]]) {
    assert.deepEqual(draftPreviewTemplate(saved, { ...unchanged, layouts }).layouts, saved.layouts);
  }
  // An empty list is a valid save: the preview falls back to its standard deck, as it will once saved.
  assert.deepEqual(draftPreviewTemplate(saved, { ...unchanged, layouts: [] }).layouts, []);
});

test("the limits count characters the way the server does, in code points", () => {
  // An emoji is ONE character to `max_length` and two UTF-16 units to `length`.
  const emoji = "\u{1F3A8}";
  const longest = emoji.repeat(MAX_TEMPLATE_NAME_CHARS);
  assert.equal(longest.length, 2 * MAX_TEMPLATE_NAME_CHARS);
  assert.equal(draftPreviewTemplate(saved, { ...unchanged, name: longest }).name, longest, "a name the save accepts");
  assert.equal(draftPreviewTemplate(saved, { ...unchanged, name: longest + emoji }).name, saved.name);
  const ratio = emoji.repeat(MAX_TEMPLATE_ASPECT_RATIO_CHARS);
  assert.equal(draftPreviewTemplate(saved, { ...unchanged, aspectRatio: ratio }).aspect_ratio, ratio);
  assert.equal(draftPreviewTemplate(saved, { ...unchanged, aspectRatio: ratio + emoji }).aspect_ratio, saved.aspect_ratio);
});

test("the limits are the server's", () => {
  const schema = read("../../../../backend/app/api/v1/schemas/template_container.py");
  const update = schema.slice(schema.indexOf("class TemplateContainerUpdate("));
  assert.match(update, new RegExp(`name: str \\| None = Field\\(default=None, min_length=1, max_length=${MAX_TEMPLATE_NAME_CHARS}\\)`));
  assert.match(update, new RegExp(`aspect_ratio: str \\| None = Field\\(default=None, max_length=${MAX_TEMPLATE_ASPECT_RATIO_CHARS}\\)`));
  assert.match(update, /layouts: list\[dict\] \| None = None/);
});

test("the unsaved-design preview reads the whole draft, the saved Preview the saved template", () => {
  const config = read("../../pages/template-detail/ConfigTab.tsx");
  assert.match(
    config,
    /draftPreviewTemplate\(template, \{\s*name: editName,\s*aspectRatio: editAspectRatio,\s*layouts: layoutsParsed\.error \? null : layoutsParsed\.value,\s*themeConfig: designThemeConfig,\s*extraMetadata: designExtraMetadata,\s*\}\)/,
  );
  assert.match(config, /template=\{\{ \.\.\.draftForPreview, supported_schemes: templateSupportedSchemes\(draftForPreview\) \}\}/);
  assert.match(read("../../pages/TemplateDetail.tsx"), /<TemplatePreviewDialog\s+template=\{template\}/);
});
