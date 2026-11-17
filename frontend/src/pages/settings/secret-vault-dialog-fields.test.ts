/**
 * The "Connect a vault" dialog follows the forms standard (#9375, design
 * standards s.2): every field label at body size through the design-system
 * Label, every help line at 12 px tied to its control, buttons at 36 px and
 * Cancel as a secondary button. Static, like the settings form kit guards:
 * the Google key file, the 1Password grants and the environment-declared
 * vaults rarely render in a test.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const FILE = readFileSync(join(process.cwd(), "src", "pages", "settings", "SecretVaultsSection.tsx"), "utf8");
/** The create / edit dialog and its field components: everything before the list row. */
const SOURCE = FILE.slice(FILE.indexOf("function CredentialInput("), FILE.indexOf("function ProviderRow("));

describe("Connect a vault dialog fields", () => {
  it("draws no field label with a raw label tag at 12 px", () => {
    expect(SOURCE).not.toMatch(/<label\b/);
  });

  it("labels every field through SettingsField, never the 12 px Input label (UX r1 B1 of #10406)", () => {
    expect(SOURCE).not.toMatch(/<Input\b[^>]*?\blabel=/);
    expect(SOURCE).not.toMatch(/helperText=/);
    expect(SOURCE.match(/<SettingsField\b/g)?.length ?? 0).toBeGreaterThanOrEqual(10);
    for (const match of SOURCE.matchAll(/<Input\b[\s\S]*?\/>/g)) {
      expect(match[0]).toMatch(/aria-labelledby=\{settingsFieldLabelId\(/);
    }
  });

  it("draws the dialog's switches as SettingsSwitchField, not a bare named Switch (B2)", () => {
    expect(SOURCE).not.toMatch(/<Switch\b/);
    expect(SOURCE).toContain('id="vault-browsable"');
    expect(SOURCE).toContain('id="vault-enabled"');
  });

  it("marks required fields from the same rule as the Missing footer (B3)", () => {
    expect(SOURCE.match(/requiredVaultFields\(form\)\.has\(field\.key\)/g)?.length).toBe(2);
    expect(SOURCE.match(/required=\{required\}/g)?.length ?? 0).toBeGreaterThanOrEqual(6);
  });

  it("keeps the 10 px caption scale for badges only", () => {
    for (const match of SOURCE.matchAll(/\bcontent-caption\b/g)) {
      const open = SOURCE.lastIndexOf("<", match.index ?? 0);
      expect(SOURCE.slice(open, open + 6)).toBe("<Badge");
    }
  });

  it("ties every help line to its control", () => {
    expect(SOURCE).toContain('aria-describedby={methodHelp ? "vault-method-help" : undefined}');
    expect(SOURCE).toContain('id="vault-method-help"');
    expect(SOURCE.match(/aria-describedby=\{helpId\}/g)?.length).toBe(6);
    expect(SOURCE).toContain("const helpId = help ? settingsFieldHintId(id) : undefined;");
  });

  it("types secrets and settings at body size, never in a 10 px textarea", () => {
    for (const match of SOURCE.matchAll(/<textarea\b[\s\S]*?\/>/g)) {
      expect(match[0]).toContain("content-base");
      expect(match[0]).toMatch(/\bid=/);
    }
  });

  it("reads the dialog's field components, not an empty slice", () => {
    expect(SOURCE.length).toBeGreaterThan(10_000);
  });

  it("names every problem that disables the save, inline and in the Missing footer (UX r2 B1 of #10406)", () => {
    const footer = SOURCE.slice(SOURCE.indexOf("function missingNames("), SOURCE.indexOf("function GrantsEditor("));
    for (const kind of ['"required"', '"name"', '"grant"']) expect(footer).toContain(`problem.kind === ${kind}`);
    expect(SOURCE).toContain("missing.length > 0 &&");
    expect(SOURCE).toMatch(/missingGrants\.has\(index\)\s*\?/);
    expect(SOURCE).toContain("<GrantsEditor form={form} problems={problems}");
  });

  it("keeps a grant row's controls on one baseline: its error goes under the whole row (UX r3 B1 of #10406)", () => {
    const grants = SOURCE.slice(SOURCE.indexOf("function GrantsEditor("));
    const row = grants.slice(grants.indexOf('data-part="grant-controls"'), grants.indexOf("missingGrants.has(index) ? ("));
    expect(row.length).toBeGreaterThan(0);
    expect(row).not.toMatch(/\berror=/);
    expect(grants).toMatch(/<\/div>\s*\{missingGrants\.has\(index\) \? \(\s*<p id=\{settingsFieldHintId\(`vault-grant-\$\{index\}`\)\} role="alert"/);
  });

  it("edits path prefixes as a list of entries, never comma-separated text (B2)", () => {
    expect(SOURCE).not.toMatch(/[Cc]omma-separated/);
    expect(SOURCE).not.toMatch(/\.split\(","\)/);
    expect(SOURCE.match(/<TagInput\b[\s\S]*?preserveCase[\s\S]*?suggestFromPlatformTags=\{false\}/g)?.length).toBe(2);
  });

  it("gives every setting field an info bubble and the KV versions their meaning (B3)", () => {
    expect(SOURCE.match(/<SettingsField id=\{id\} label=\{name\} info=\{info\}/g)?.length).toBe(6);
    expect(SOURCE).toContain("VAULT_KV_VERSION_DESCRIPTIONS");
    expect(SOURCE).toContain('label={t_i18n("About this field")}');
  });

  it("draws a stored credential at the 36 px height of an input, its buttons outside the box (A1 of #10406 r4)", () => {
    const row = SOURCE.slice(SOURCE.indexOf('data-part="stored-credential"'), SOURCE.indexOf('{t_i18n("Replace")}'));
    expect(row.length).toBeGreaterThan(0);
    expect(row).not.toMatch(/py-1\.5/);
    expect(row).toMatch(/<span className="flex h-9 [^"]*border[^"]*">[\s\S]*?<\/span>\s*<Button/);
  });

  it("fills the stored credential box like an input and words it as a state (A1-A2 of #10539 r1)", () => {
    const row = SOURCE.slice(SOURCE.indexOf('data-part="stored-credential"'), SOURCE.indexOf('{t_i18n("Replace")}'));
    expect(row).toMatch(/<span className="flex h-9 [^"]*border-transparent bg-input-default[^"]*">/);
    expect(row).toContain('t_i18n("Set (stored credential)")');
  });

  it("draws the dialog's buttons at 36 px with Cancel secondary", () => {
    // size="sm" is refused on the buttons; the library Spinner's own sm size is not a button size.
    expect(SOURCE).not.toMatch(/(?<!<Spinner )size="sm"/);
    expect(SOURCE).not.toMatch(/className="h-7"/);
    expect(SOURCE).toMatch(/variant="secondary" onClick=\{onClose\}/);
  });
});
