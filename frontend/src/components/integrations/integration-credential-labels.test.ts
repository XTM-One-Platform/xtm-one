/** Integration credential fields share one label style with the SecretField.
 *
 *  The SecretField draws its label with the design-system `Label` (14 px,
 *  primary). The Client ID, Username and Password fields beside it on the
 *  integration forms used a 12 px secondary `<label>`, and the Configuration
 *  card's secrets were `compact` (32 px) next to a 36 px Client ID. Every
 *  credential label of these forms is now the `Label`, at full field height,
 *  and the OpenAPI help lines sit at the 12 px help size.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path: string) => readFileSync(join(SRC, path), "utf8");

const configurationCard = read("pages/integration-detail/ConfigurationCard.tsx");
const newIntegration = read("pages/NewIntegration.tsx");
const openapiAuthFields = read("components/integrations/openapi/openapi-auth-fields.tsx");
const openapiCertificateFields = read("components/integrations/openapi/openapi-certificate-fields.tsx");
const relayRouteField = read("components/ui/relay-route-field.tsx");
const extraFieldInputs = read("components/ui/extra-field-inputs.tsx");

// The create forms' company-managed toggle: its help line at the 12 px help size.
const COMPANY_MANAGED_FORMS = [
  "pages/NewIntegration.tsx",
  "pages/NewAgent.tsx",
  "pages/NewKnowledgeBase.tsx",
  "pages/NewTemplate.tsx",
  "pages/custom-tool-detail/NewToolForm.tsx",
  "pages/mcp-server-detail/NewServerForm.tsx",
  "pages/prompt-detail/NewPromptForm.tsx",
  "pages/skill-detail/NewSkillForm.tsx",
  "components/presentation/PresentationShareDialog.tsx",
  "components/ui/entity-share-dialog.tsx",
];

// Every field label of these forms: one scale (the design-system Label), no
// 12 px `<label>` left beside it, no 11 px help line, no 32 px control.
const ANY_SMALL_LABEL = /<label className="content-compact[^"]*text-default-secondary">/;
const FORM_FILES = {
  configurationCard,
  newIntegration,
  openapiAuthFields,
  openapiCertificateFields,
  relayRouteField,
  extraFieldInputs,
};

const SMALL_LABEL = /<label className="content-compact text-default-secondary">\{t_i18n\("(Client ID|Username|Password)"\)\}<\/label>/;

describe("integration credential labels match the SecretField label", () => {
  it("labels Client ID, Username and Password with the design-system Label on the Configuration card", () => {
    expect(configurationCard).not.toMatch(SMALL_LABEL);
    for (const name of ["Client ID", "Username", "Password"]) {
      expect(configurationCard).toContain(`<Label className="block text-default-primary">{t_i18n("${name}")}</Label>`);
    }
  });

  it("draws the Configuration card's secrets at full field height", () => {
    const secretFields = configurationCard.match(/<SecretField\b[\s\S]*?\/>/g) ?? [];
    expect(secretFields.length).toBeGreaterThan(0);
    for (const field of secretFields) expect(field).not.toMatch(/\bcompact\b/);
    for (const name of ["Client ID", "Username", "Password"]) {
      const input = configurationCard.match(new RegExp(`<Input aria-label=\\{t_i18n\\("${name}"\\)\\}[^\\n]*/>`))?.[0] ?? "";
      expect(input).not.toBe("");
      expect(input).not.toContain("h-8");
    }
  });

  it("labels Client ID with the design-system Label on New integration", () => {
    expect(newIntegration).not.toMatch(SMALL_LABEL);
    expect(newIntegration).toContain('<Label className="block text-default-primary">{t_i18n("Client ID")}</Label>');
  });

  it("labels OpenAPI auth fields with the Label and keeps help lines at 12 px", () => {
    expect(openapiAuthFields).toContain('<Label className="block text-default-primary">{label}</Label>');
    expect(openapiAuthFields).not.toContain("text-[11px]");
  });

  it.each(Object.entries(FORM_FILES))("%s has no 12 px secondary field label", (_name, source) => {
    expect(source).not.toMatch(ANY_SMALL_LABEL);
  });

  it("labels Name, Tags, Description and Authentication mode with the Label on both forms", () => {
    for (const source of [configurationCard, newIntegration]) {
      for (const name of ["Name", "Tags", "Authentication mode"]) {
        expect(source).toContain(`<Label className="block text-default-primary">{t_i18n("${name}")}`);
      }
    }
    expect(configurationCard).toContain('<Label className="block text-default-primary">{t_i18n("Description")}');
    // Route through is a SettingsField: the design-system Label tied to the picker.
    expect(relayRouteField).toContain('label={t_i18n("Route through")}');
    expect(relayRouteField).toContain("aria-labelledby={settingsFieldLabelId(pickerId)}");
  });

  it("draws the Configuration card's Name and Authentication mode at full field height", () => {
    expect(configurationCard).not.toMatch(/<SelectTrigger className="h-8/);
    const name = configurationCard.match(/<Input aria-label=\{t_i18n\("Name"\)\}[^\n]*\/>/)?.[0] ?? "";
    expect(name).not.toBe("");
    expect(name).not.toContain("h-8");
  });

  it("labels provider extra fields with the Label at full field height", () => {
    expect(extraFieldInputs).toContain('<Label htmlFor={htmlFor} className="text-default-primary">{label.name}</Label>');
    expect(extraFieldInputs).toMatch(/<ExtraFieldLabelRow htmlFor=\{`extra-field-\$\{key\}`\} label=\{label\} \/>/);
    expect(extraFieldInputs).not.toContain("h-8");
  });

  it("gives provider extra fields an info bubble, an Optional mark and a human placeholder, never the raw key", () => {
    expect(extraFieldInputs).toContain('<InfoTip label={t_i18n("About {field}", { field: label.name })} content={label.detail} />');
    expect(extraFieldInputs).toContain('{t_i18n("Optional")}');
    expect(extraFieldInputs).not.toMatch(/: key\}/);
    expect(extraFieldInputs).toContain('helpFor?.(key)');
    expect(extraFieldInputs).toContain("choices ? undefined");
    expect(extraFieldInputs).not.toMatch(/<p className="content-compact text-default-secondary">\{desc\}/);
  });

  it("labels the approval switch with the Label on the Configuration card", () => {
    expect(configurationCard).toMatch(/<Label htmlFor="requires-approval" className="text-default-primary">/);
    expect(configurationCard).not.toMatch(/<label htmlFor="requires-approval"/);
  });

  it("keeps New integration help lines at 12 px or more", () => {
    expect(newIntegration).not.toMatch(/<(p|span|button) className="[^"]*text-\[1[01]px\]/);
    expect(newIntegration).not.toMatch(/<span className="[^"]*content-caption text-default-secondary">/);
  });

  it.each(COMPANY_MANAGED_FORMS)("%s draws the company-managed help at 12 px", (path) => {
    expect(read(path)).not.toMatch(/<p className="text-\[11px\] text-default-secondary">\s*\{t_i18n\("(Shared with all users|Visible to everyone in the company)\. Only admins can edit\."\)/);
  });

  it("keeps the relay route and certificate help lines at 12 px", () => {
    expect(relayRouteField).not.toContain("text-[11px]");
    expect(openapiCertificateFields).not.toMatch(/<p className="text-\[11px\]/);
  });
});
