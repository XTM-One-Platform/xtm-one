/**
 * Print the editor's block / slide catalog as JSON - every field
 * descriptor, every palette default (`create()`), every item default - for
 * the backend tripwire (`tests/test_presentation_editor.py`) that validates
 * them against the Pydantic models. Run with `node scripts/dump-presentation-schema.mts`
 * (Node strips the types itself; no build, no node_modules).
 */
import { BLOCK_SCHEMA, INSERTABLE_BLOCK_TYPES, blockAllowedIn } from "../src/components/presentation/block-schema.ts";
import { SLIDE_SCHEMA } from "../src/components/presentation/slide-schema.ts";

const blocks: Record<string, unknown> = {};
for (const [type, schema] of Object.entries(BLOCK_SCHEMA)) {
  const created = schema.create?.();
  blocks[type] = {
    label: schema.label,
    fields: schema.fields,
    items: schema.items
      ? {
          key: schema.items.key,
          min: schema.items.min,
          max: schema.items.max,
          fields: schema.items.fields,
          // `null`: a list the inspector never grows by hand (a logo needs a file).
          item: schema.items.create ? schema.items.create() : null,
          unset_when_empty: schema.items.unsetWhenEmpty ?? false,
        }
      : null,
    table: schema.table ?? false,
    create: created ?? null,
    allowed_in: {
      body: blockAllowedIn(type as never, "blocks"),
      column: blockAllowedIn(type as never, "columns.0"),
      callout: blockAllowedIn(type as never, "blocks.0.children"),
      comparison: blockAllowedIn(type as never, "blocks.0.left.blocks"),
    },
  };
}

const slides: Record<string, unknown> = {};
for (const [layout, schema] of Object.entries(SLIDE_SCHEMA)) {
  slides[layout] = {
    fields: schema.fields,
    items: schema.items
      ? {
          key: schema.items.key,
          min: schema.items.min,
          max: schema.items.max,
          fields: schema.items.fields,
          item: schema.items.create ? schema.items.create() : null,
          unset_when_empty: schema.items.unsetWhenEmpty ?? false,
        }
      : null,
    create: schema.create(2),
  };
}

process.stdout.write(JSON.stringify({ insertable: INSERTABLE_BLOCK_TYPES, blocks, slides }));
