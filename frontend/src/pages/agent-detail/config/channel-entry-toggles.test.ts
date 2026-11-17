/**
 * Agent > Configuration > Channels & Bots and Assignment > Channels: the
 * per-channel toggles follow the form standard (#9375, Design standards s.2)
 * - `yarn test`.
 *
 * Each toggle is a SettingsSwitchField: a 14 px label tied to the switch by
 * aria-labelledby, a 12 px help line tied by aria-describedby and an info
 * bubble. The old rows rendered 11 px labels, 8 and 9 px captions and a
 * switch named by aria-label only.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const read = (...parts: string[]) => readFileSync(join(process.cwd(), "src", "pages", ...parts), "utf8");

const SCREENS: Array<[string, string, string[]]> = [
  [
    "agent ChannelsSection",
    read("agent-detail", "config", "ChannelsSection.tsx"),
    [
      "`agent-${agent.id}-web-enabled`",
      "`agent-${agent.id}-web-uploads`",
      "`agent-channel-${chType}-enabled`",
      "`agent-channel-${chType}-bypass-whitelist`",
      "`agent-channel-${chType}-receive`",
      "`agent-channel-${chType}-allow-anonymous`",
      "`agent-channel-${chType}-allow-private`",
      "`agent-channel-${chType}-only-mention`",
      "`agent-channel-${chType}-publish`",
    ],
  ],
  [
    "assignment ChannelsTab",
    read("assignment-detail", "ChannelsTab.tsx"),
    [
      "`assignment-${assignment.id}-channels-custom-scope`",
      "`assignment-channel-${chType}-enabled`",
      "`assignment-channel-${chType}-receive`",
      "`assignment-channel-${chType}-publish`",
    ],
  ],
];

describe.each(SCREENS)("%s channel toggles", (_name, source, ids) => {
  it.each(ids)("renders %s as a SettingsSwitchField with a helper and an info bubble", (id) => {
    const start = source.indexOf(`id={${id}}`);
    expect(start).toBeGreaterThan(-1);
    const open = source.lastIndexOf("<SettingsSwitchField", start);
    expect(open).toBeGreaterThan(-1);
    expect(source.slice(open, start)).not.toContain("/>");
    const block = source.slice(start, source.indexOf("onCheckedChange=", start));
    expect(block).toMatch(/\bhelper=/);
    expect(block).toMatch(/\binfo=\{t_i18n\(/);
  });

  it("names no switch by aria-label alone", () => {
    expect(source).not.toMatch(/<Switch\b/);
  });

  it("carries no 11 px label in a toggle row", () => {
    expect(source).not.toMatch(/text-\[11px\] font-medium/);
  });

  it("ties every channel picker to its visible caption", () => {
    const pickers = source.split("<ChannelPicker").slice(1);
    expect(pickers.length).toBeGreaterThan(0);
    for (const picker of pickers) {
      const props = picker.slice(0, picker.indexOf("/>"));
      const match = props.match(/ariaLabelledBy=\{`([^`]+)`\}/);
      expect(match).not.toBeNull();
      expect(source).toContain(`id={\`${match![1]}\`}`);
    }
  });
});

describe("ChannelPicker", () => {
  const picker = readFileSync(join(process.cwd(), "src", "components", "ui", "channel-picker.tsx"), "utf8");

  it("translates every string it renders", () => {
    for (const text of ["Loading channels...", "No matching channels.", "All channels selected.", "No channels selected", "Search channels..."]) {
      expect(picker).toContain(`t_i18n("${text}")`);
      expect(picker).not.toMatch(new RegExp(`[^(]"${text.replace(/\./g, "\\.")}"`));
    }
  });

  it("wires aria-labelledby on the search field", () => {
    expect(picker).toMatch(/aria-labelledby=\{ariaLabelledBy\}/);
  });
});
