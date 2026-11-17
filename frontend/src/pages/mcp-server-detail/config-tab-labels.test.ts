/**
 * Guard for the MCP server Configuration edit view (form standard sweep,
 * #9375, slice 11): every field is a SettingsField (14 px label tied to its
 * control, 12 px help line tied by aria-describedby, info bubble where the
 * create form has one), no 32 px input, no 10 or 11 px text, "Hide from chat"
 * a SettingsSwitchField.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(HERE, "ConfigTab.tsx"), "utf8");
const editView = source.slice(source.indexOf("editView={"), source.indexOf("<SettingsSwitchField"));

const FIELDS = [
  "edit-server-name",
  "edit-server-tags",
  "edit-server-command",
  "edit-server-args",
  "edit-server-url",
  "edit-server-authorize-url",
  "edit-server-token-url",
  "edit-server-registration-url",
  "edit-server-scopes",
  "edit-server-client-id",
  "edit-server-client-secret",
];

describe("MCP server Configuration edit fields", () => {
  it("renders no raw <label>, no 32 px input and no text below 12 px in the edit view", () => {
    expect(editView.length).toBeGreaterThan(0);
    expect(editView).not.toMatch(/<label\b/);
    expect(editView).not.toMatch(/className="h-8/);
    expect(editView).not.toMatch(/content-caption|text-\[(8|9|10|11)px\]/);
    expect(editView).not.toMatch(/size="sm"/);
    expect(editView).not.toMatch(/^\s+compact$/m);
  });

  it.each(FIELDS)("%s is a SettingsField whose control is described by its help line", (id) => {
    expect(source).toContain(`<SettingsField\n`);
    expect(source).toMatch(new RegExp(`id="${id}"[\\s\\S]{0,40}label=|<SettingsField id="${id}"`));
    expect(source).toContain(`aria-describedby={settingsFieldHintId("${id}")}`);
  });

  it("Hide from chat is a SettingsSwitchField with its help line", () => {
    expect(source).toMatch(/<SettingsSwitchField\s+id="edit-server-hide-from-chat"/);
    expect(source).not.toMatch(/text-\[11px\] text-default-secondary">\{t_i18n\("When enabled/);
  });
});
