/**
 * Unit tests for the editor's catalogs - `yarn test:unit`.
 *
 * What the Pydantic models cannot check from here is checked by the backend
 * (`tests/test_presentation_editor.py` runs `scripts/dump-presentation-schema.mts`
 * and validates every default). Pinned here: the catalog's own consistency -
 * every palette default is a block of its type with its item list within the
 * bounds the schema declares, every required text field of a default is
 * filled, the nesting rules read as the spec nests blocks.
 */

import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

import { capTipText } from "../../lib/tip-text.ts";

import {
  BLOCK_SCHEMA,
  INSERTABLE_BLOCK_TYPES,
  blockAllowedIn,
  enumShown,
  enumWritten,
  type SchemaField,
} from "./block-schema.ts";
import { SLIDE_SCHEMA } from "./slide-schema.ts";

const requiredTextKeys = (fields: SchemaField[]) =>
  fields.filter((field) => field.kind === "text" && !field.optional).map((field) => field.key);

test("an enum that explains its options explains every one of them, and the image frame does", () => {
  for (const [type, schema] of Object.entries(BLOCK_SCHEMA)) {
    for (const field of schema.fields) {
      if (field.kind !== "enum" || !field.optionHints) continue;
      assert.deepEqual(
        Object.keys(field.optionHints).sort(),
        field.options.map(String).sort(),
        `${type}.${field.key}: one meaning line per option`,
      );
    }
  }
  const frame = BLOCK_SCHEMA.image.fields.find((field) => field.key === "frame");
  assert.ok(frame?.kind === "enum" && frame.optionHints && frame.unsetHint && frame.info, "image.frame explains itself");
});

test("the poll mode options are one word with their meaning line under them, so no translation is cut", () => {
  const mode = BLOCK_SCHEMA.poll.fields.find((field) => field.key === "layout");
  assert.ok(mode?.kind === "enum" && mode.optionLabels && mode.optionHints, "poll.layout explains its options");
  for (const label of Object.values(mode.optionLabels)) {
    assert.ok(!/[\s:]/.test(label), `poll.layout option "${label}" is one word`);
  }
});

test("every poll option is a short label with its meaning line, so no translation is cut", () => {
  for (const key of ["show_results", "layout", "controls", "reveal"]) {
    const field = BLOCK_SCHEMA.poll.fields.find((candidate) => candidate.key === key);
    assert.ok(field?.kind === "enum" && field.optionLabels && field.optionHints, `poll.${key} explains its options`);
    for (const label of Object.values(field.optionLabels)) {
      assert.ok(label.length <= 16 && !/[,:]/.test(label), `poll.${key} option "${label}" is short`);
      // A generic one-word key ("None", "Never") is shared app-wide and cannot agree in gender
      // or meaning with this field in every language (UX #10793 r1 A2): name the option.
      assert.ok(!["None", "Never"].includes(label), `poll.${key} option "${label}" has its own key`);
    }
    // A meaning line adds a fact: it never only restates its label (UX #10793 r1 A3).
    for (const [option, hint] of Object.entries(field.optionHints)) {
      const label = field.optionLabels[option] ?? "";
      assert.ok(!hint.toLowerCase().startsWith(label.toLowerCase()), `poll.${key}.${option} hint repeats its label`);
    }
  }
});

test("every info bubble fits the Tip whole in en, fr, es and ja, so no translation is cut", () => {
  const catalogs = ["en", "fr", "es", "ja"].map((lang) => ({
    lang,
    strings: JSON.parse(readFileSync(new URL(`../../i18n/lang/${lang}.json`, import.meta.url), "utf8")) as Record<string, string>,
  }));
  for (const [type, schema] of Object.entries(BLOCK_SCHEMA)) {
    for (const field of [...schema.fields, ...(schema.items?.fields ?? [])]) {
      const info = (field as { info?: string }).info;
      if (!info) continue;
      for (const { lang, strings } of catalogs) {
        const text: string = strings[info] ?? info;
        assert.equal(capTipText(text), text, `${type}.${field.key} info in ${lang} is ${text.length} characters, cut by the Tip`);
      }
    }
  }
});

test("every palette default is a block of its own type, its items within the declared bounds", () => {
  for (const type of INSERTABLE_BLOCK_TYPES) {
    const schema = BLOCK_SCHEMA[type];
    const block = schema.create!() as unknown as Record<string, unknown>;
    assert.equal(block.type, type);
    for (const key of requiredTextKeys(schema.fields)) {
      assert.ok(typeof block[key] === "string" && block[key], `${type}.${key} must be filled in the default`);
    }
    if (schema.items) {
      // An optional list (`min: 0`) may be left out of the default altogether.
      const items = block[schema.items.key] ?? [];
      assert.ok(Array.isArray(items), `${type}.${schema.items.key} must be a list`);
      assert.ok(items.length >= schema.items.min && items.length <= schema.items.max, `${type} default has ${items.length} items`);
      // An insertable block's list is one the inspector may grow.
      assert.ok(schema.items.create, `${type}.${schema.items.key} has no item factory`);
      const item = schema.items.create() as Record<string, unknown>;
      for (const key of requiredTextKeys(schema.items.fields)) {
        assert.ok(typeof item[key] === "string" && item[key], `${type} item default must fill ${key}`);
      }
    }
  }
});

test("blocks that need a file, numbers or a graph are not offered by hand", () => {
  for (const type of ["image", "video", "logo_grid", "logo_wall", "chart", "diagram", "architecture"] as const) {
    assert.equal(BLOCK_SCHEMA[type].create, undefined, type);
    assert.ok(!INSERTABLE_BLOCK_TYPES.includes(type));
  }
  // A logo needs a file the inspector cannot provide: its list is edited,
  // reordered and trimmed, never grown by hand (an added `{file_id: ""}`
  // would fail the save) - and a wall's group needs its logos.
  assert.equal(BLOCK_SCHEMA.logo_grid.items?.create, undefined);
  assert.equal(BLOCK_SCHEMA.logo_wall.items?.create, undefined);
});

test("the nesting rules follow the spec: containers hold leaf or atomic blocks only", () => {
  // A slide body takes everything.
  assert.equal(blockAllowedIn("columns", "blocks"), true);
  assert.equal(blockAllowedIn("callout", "blocks"), true);
  // A column (of the slide or of a columns block) takes leaf blocks: no columns.
  assert.equal(blockAllowedIn("columns", "columns.0"), false);
  assert.equal(blockAllowedIn("columns", "blocks.2.columns.1"), false);
  assert.equal(blockAllowedIn("callout", "columns.0"), true);
  // A callout's children and a comparison side take atomic blocks only.
  for (const container of ["callout", "comparison", "columns"] as const) {
    assert.equal(blockAllowedIn(container, "blocks.0.children"), false, container);
    assert.equal(blockAllowedIn(container, "blocks.0.left.blocks"), false, container);
    assert.equal(blockAllowedIn(container, "blocks.0.right.blocks"), false, container);
  }
  assert.equal(blockAllowedIn("text", "blocks.0.children"), true);
  assert.equal(blockAllowedIn("list", "blocks.0.right.blocks"), true);
});

test("a look read in normal form is shown as its default and written back unset", () => {
  const field = (type: "metric" | "metrics_grid", key: string) => BLOCK_SCHEMA[type].fields.find((f) => f.key === key)!;
  for (const type of ["metric", "metrics_grid"] as const) {
    for (const [key, fallback, other] of [
      ["variant", "card", "plain"],
      ["align", "start", "center"],
    ] as const) {
      const look = field(type, key);
      assert.ok(look.kind === "enum" && look.defaultValue === fallback, `${type}.${key}`);
      assert.equal(enumShown(look, undefined), fallback, `${type}.${key} unset reads as the default`);
      assert.equal(enumShown(look, other), other);
      assert.equal(enumWritten(look, fallback), undefined, `${type}.${key} = ${fallback} is written unset`);
      assert.equal(enumWritten(look, other), other);
    }
  }
  // Every other enum keeps its "Default" entry (unset) and writes what is picked.
  const size = BLOCK_SCHEMA.text.fields.find((f) => f.key === "size")!;
  assert.equal(enumShown(size, undefined), undefined);
  assert.equal(enumWritten(size, "md"), "md");
  assert.equal(enumWritten(size, undefined), undefined);
});

test("every layout's default is a slide of that layout with its items within bounds", () => {
  for (const [layout, schema] of Object.entries(SLIDE_SCHEMA)) {
    const slide = schema.create(3) as unknown as Record<string, unknown>;
    assert.equal(slide.layout, layout);
    for (const key of requiredTextKeys(schema.fields)) {
      assert.ok(typeof slide[key] === "string" && slide[key], `${layout}.${key} must be filled in the default`);
    }
    if (schema.items) {
      const items = slide[schema.items.key];
      const count = Array.isArray(items) ? items.length : 0;
      assert.ok(count >= schema.items.min && count <= schema.items.max, `${layout} default has ${count} ${schema.items.key}`);
    }
  }
});
