/**
 * Settings at rest are a read view (#10855): a settings card shows its values
 * as label / value tiles until Edit is clicked, never as disabled inputs,
 * selects, switches or textareas. A control disabled on the edit flag
 * (`disabled={!editing}`) under pages/settings is that defect; and a country
 * is a bundled SVG flag (CountryFlag), never a regional-indicator emoji built
 * with String.fromCodePoint, which Windows draws as two letters.
 */
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "../src");
const SETTINGS = path.join(SRC, "pages/settings");

async function sources(dir: string): Promise<string[]> {
  const out: string[] = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await sources(full)));
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

describe("settings read view gate (#10855)", () => {
  it("no settings control is disabled on the edit flag: the card reads as tiles at rest", async () => {
    const offenders: string[] = [];
    for (const file of await sources(SETTINGS)) {
      const text = await readFile(file, "utf8");
      if (/disabled=\{\s*!\s*(editing|isEditing)\s*\}/.test(text)) offenders.push(path.relative(SETTINGS, file));
    }
    expect(offenders, "render SettingsReadField at rest, the form only while editing").toEqual([]);
  });

  it("an empty settings value falls back to a muted wording, never a bare Product default read like a set value", async () => {
    const offenders: string[] = [];
    for (const file of await sources(SETTINGS)) {
      const text = await readFile(file, "utf8");
      // A bare fallback (`|| t_i18n("Product default")`, `: t_i18n("Product default")`) draws the
      // default in primary text; SettingsNotSet label= draws it muted (UX round 1 on #10870, B1).
      if (/(\|\||[?:])\s*t_i18n\(\s*"Product default"\s*\)/.test(text)) offenders.push(path.relative(SETTINGS, file));
    }
    expect(offenders, "wrap the fallback in <SettingsNotSet label={...} />").toEqual([]);
  });

  it("no country flag is drawn from regional-indicator emoji", async () => {
    const offenders: string[] = [];
    for (const file of await sources(SRC)) {
      const text = await readFile(file, "utf8");
      if (/0x1f1e6|127397/i.test(text)) offenders.push(path.relative(SRC, file));
    }
    expect(offenders, "use CountryFlag from @/components/ui/country-flag").toEqual([]);
  });
});
