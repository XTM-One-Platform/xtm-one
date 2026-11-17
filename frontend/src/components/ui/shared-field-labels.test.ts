/**
 * Guard for the shared field components used by many forms (form standard
 * sweep, #9375): KeyValueEditor (New MCP server env vars and headers) and
 * RelayRouteField ("Route through"). Each field label is the design-system
 * Label tied to its control by id, the help lines are 12 px or more and are
 * tied to the control by aria-describedby.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (file: string) => readFileSync(join(HERE, file), "utf8");

describe("shared field labels", () => {
  it.each(["key-value-editor.tsx", "relay-route-field.tsx"])(
    "%s has no raw <label> and no help line below 12 px",
    (file) => {
      const source = read(file);
      expect(source).not.toMatch(/<label\b/);
      expect(source).not.toMatch(/text-\[(8|9|10|11)px\]/);
    },
  );

  it("KeyValueEditor ties each key and value label to its input and the row description", () => {
    const source = read("key-value-editor.tsx");
    expect(source).toContain("<Label id={`${keyId}-label`} htmlFor={keyId}");
    expect(source).toContain("<Label id={`${valueId}-label`} htmlFor={valueId}");
    expect(source).toContain("aria-labelledby={`${keyId}-label`}");
    expect(source).toContain("aria-labelledby={`${valueId}-label`}");
    expect(source).toContain("<Input id={keyId}");
    expect(source).toContain("<Input id={valueId}");
    expect(source.match(/aria-describedby=\{describedBy\}/g)?.length).toBe(2);
    expect(source).toContain("const describedBy = [helperId, descriptionId]");
    expect(source).toContain('role="group" aria-labelledby={groupLabelId}');
    expect(source).not.toMatch(/content-caption/);
  });

  it("KeyValueEditor carries a help line and an info bubble beside its title", () => {
    const source = read("key-value-editor.tsx");
    expect(source).toContain("<p id={helperId} className=\"-mt-1 content-compact text-default-secondary\">");
    expect(source).toContain('label={t_i18n("About this field")}');
    for (const file of ["../../pages/mcp-server-detail/NewServerForm.tsx", "../../pages/mcp-server-detail/ConfigTab.tsx"]) {
      const page = read(file);
      const editors = page.match(/<KeyValueEditor[\s\S]*?\/>/g) ?? [];
      expect(editors.length).toBe(2);
      for (const editor of editors) {
        expect(editor).toMatch(/\bhelper="/);
        expect(editor).toMatch(/\binfo="/);
      }
    }
  });

  it("RelayRouteField ties the Route through label and its help line to the picker", () => {
    const source = read("relay-route-field.tsx");
    expect(source).toContain("<SettingsField\n        id={pickerId}");
    expect(source).toContain("aria-labelledby={settingsFieldLabelId(pickerId)}");
    expect(source).toContain("aria-describedby={describedBy}");
    expect(source).not.toContain('aria-label={t_i18n("Route through")}');
  });

  it("RelayRouteField warnings are 12 px and announced with the picker", () => {
    const source = read("relay-route-field.tsx");
    expect(source).not.toMatch(/content-caption/);
    expect(source).toContain("settingsFieldHintId(pickerId),\n    noneOnline && offlineId,\n    destinationWarning && destinationId,");
    expect(source).toContain("<p id={offlineId}");
    expect(source.match(/<p id=\{destinationId\}/g)?.length).toBe(2);
  });
});
