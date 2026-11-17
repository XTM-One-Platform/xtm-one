// Recent health tile (assignment Overview): the value is "{completed}, {failed}"
// in a 24 px bold StatCard clamped to two lines. At 1024 with the navigation
// rail expanded the value box is 149 px wide, so the completed segment with
// its comma must fit on the first line and the failed segment on the second,
// for counts up to 10 (the hook caps the run list at 10). Otherwise the clamp
// hides the failed segment: es "10 completadas,", then
// en "10 completed," and fr "10 terminées,".
//
// jsdom cannot lay text out, so this guard measures the text itself: the sum of
// the advance widths of the shipped IBM Plex Sans 700 WOFF (the font the value
// is served in), at 24 px. A glyph the font lacks (ja) counts as 1 em, the
// width of a CJK fallback glyph. Kerning is left out: it only narrows these
// strings, so the budget errs on the safe side.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { inflateSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import { parse, TYPE, type MessageFormatElement } from "@formatjs/icu-messageformat-parser";

import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

const COMPLETED = "{count, plural, one {# completed} other {# completed}}";
const FAILED = "{count, plural, one {# failed} other {# failed}}";
const CATALOGS: Record<string, Record<string, string>> = { en, es, fr, ja };
const VALUE_BOX_PX = 149;
const FONT_SIZE_PX = 24;

// Advance widths of a WOFF 1.0 font: the head, hhea, cmap (format 4) and hmtx tables.
function loadAdvances(path: string) {
  const woff = readFileSync(path);
  const tables = new Map<string, Buffer>();
  const numTables = woff.readUInt16BE(12);
  for (let i = 0; i < numTables; i++) {
    const at = 44 + i * 20;
    const tag = woff.toString("latin1", at, at + 4);
    const offset = woff.readUInt32BE(at + 4);
    const compLength = woff.readUInt32BE(at + 8);
    const origLength = woff.readUInt32BE(at + 12);
    const raw = woff.subarray(offset, offset + compLength);
    tables.set(tag, compLength < origLength ? inflateSync(raw) : raw);
  }
  const table = (tag: string) => {
    const data = tables.get(tag);
    if (!data) throw new Error(`no ${tag} table in ${path}`);
    return data;
  };
  const unitsPerEm = table("head").readUInt16BE(18);
  const numberOfHMetrics = table("hhea").readUInt16BE(34);
  const hmtx = table("hmtx");
  const cmap = table("cmap");
  let sub = -1;
  for (let i = 0; i < cmap.readUInt16BE(2); i++) {
    const rec = 4 + i * 8;
    if (cmap.readUInt16BE(rec) === 3 && cmap.readUInt16BE(rec + 2) === 1) sub = cmap.readUInt32BE(rec + 4);
  }
  if (sub < 0 || cmap.readUInt16BE(sub) !== 4) throw new Error(`no Unicode BMP cmap in ${path}`);
  const segCount = cmap.readUInt16BE(sub + 6) / 2;
  const ends = sub + 14;
  const starts = ends + segCount * 2 + 2;
  const deltas = starts + segCount * 2;
  const rangeOffsets = deltas + segCount * 2;
  const glyphOf = (code: number): number => {
    for (let s = 0; s < segCount; s++) {
      if (code > cmap.readUInt16BE(ends + s * 2)) continue;
      const start = cmap.readUInt16BE(starts + s * 2);
      if (code < start) return 0;
      const delta = cmap.readUInt16BE(deltas + s * 2);
      const rangeAt = rangeOffsets + s * 2;
      const rangeOffset = cmap.readUInt16BE(rangeAt);
      if (rangeOffset === 0) return (code + delta) & 0xffff;
      const glyph = cmap.readUInt16BE(rangeAt + rangeOffset + (code - start) * 2);
      return glyph === 0 ? 0 : (glyph + delta) & 0xffff;
    }
    return 0;
  };
  const advance = (glyph: number) => hmtx.readUInt16BE(Math.min(glyph, numberOfHMetrics - 1) * 4);
  return (text: string, sizePx: number) => {
    let units = 0;
    for (const char of text) {
      const glyph = glyphOf(char.codePointAt(0) ?? 0);
      units += glyph === 0 ? unitsPerEm : advance(glyph);
    }
    return (units * sizePx) / unitsPerEm;
  };
}

const require = createRequire(import.meta.url);
const widthOf = loadAdvances(require.resolve("@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-700-normal.woff"));

// Renders every plural branch of the message for count 10 as plain text.
const branchTexts = (message: string): string[] => {
  const texts: string[] = [];
  for (const el of parse(message)) {
    if (el.type !== TYPE.plural) continue;
    for (const option of Object.values(el.options)) {
      texts.push(
        option.value
          .map((part: MessageFormatElement) => {
            if (part.type === TYPE.literal) return part.value;
            if (part.type === TYPE.pound) return "10";
            return "";
          })
          .join(""),
      );
    }
  }
  // ja carries no plural: the message is "{count}件完了".
  if (texts.length === 0) texts.push(message.replace("{count}", "10"));
  return texts;
};

describe("Recent health value segments fit the 1024 tile", () => {
  it("measures the font it is meant to (es 10 correctas, about 147 px)", () => {
    expect(widthOf("10 correctas,", FONT_SIZE_PX)).toBeGreaterThan(140);
    expect(widthOf("10 completed,", FONT_SIZE_PX)).toBeGreaterThan(VALUE_BOX_PX);
  });

  for (const [locale, catalog] of Object.entries(CATALOGS)) {
    // The completed segment comes first and carries the separator; the failed
    // segment ends the value.
    for (const [key, suffix] of [[COMPLETED, ","], [FAILED, ""]] as const) {
      it(`${locale}: ${key}`, () => {
        const message = catalog[key];
        expect(message, `missing in ${locale}`).toBeTruthy();
        for (const text of branchTexts(message)) {
          const width = widthOf(`${text}${suffix}`, FONT_SIZE_PX);
          expect(width, `"${text}${suffix}" in ${locale}: ${width.toFixed(1)} px`).toBeLessThanOrEqual(VALUE_BOX_PX);
        }
      });
    }
  }
});
