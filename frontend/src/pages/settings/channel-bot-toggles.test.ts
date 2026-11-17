/**
 * Settings > Integrations > Channels, the per-bot setting toggles follow the
 * form standard (#9375, Design standards s.2) - `yarn test`.
 *
 * Each toggle is a SettingsSwitchField: a 14 px label tied to the switch by
 * aria-labelledby, a 12 px help line tied by aria-describedby and an info
 * bubble with the full explanation. The old rows rendered a 12 px label, a
 * 10 px description and a switch named by aria-label only.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SOURCE = readFileSync(join(process.cwd(), "src", "pages", "settings", "ChannelManagement.tsx"), "utf8");

const TOGGLES = ["allow-anonymous", "allow-private", "requires-approval", "only-mention"];

describe("channel bot toggles", () => {
  it.each(TOGGLES)("renders %s as a SettingsSwitchField with a helper and an info bubble", (key) => {
    const start = SOURCE.indexOf(`id={\`channel-bot-\${existing.id}-${key}\`}`);
    expect(start).toBeGreaterThan(-1);
    const open = SOURCE.lastIndexOf("<SettingsSwitchField", start);
    expect(open).toBeGreaterThan(-1);
    const block = SOURCE.slice(open, SOURCE.indexOf("/>\n", SOURCE.indexOf("disabled=", start)));
    expect(block).toMatch(/\bhelper=\{t_i18n\(/);
    expect(block).toMatch(/\binfo=\{t_i18n\(/);
  });

  it("names no switch by aria-label alone", () => {
    expect(SOURCE).not.toMatch(/<Switch\b/);
  });
});
