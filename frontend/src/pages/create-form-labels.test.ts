/**
 * Create forms and the integration Email signature card render their field
 * labels with the design-system Label (14 px / 500, primary text) and their
 * help lines at 12 px (content-compact), per the form standard of
 * dev-docs/reference/frontend-design-system.md ("Forms", #9375).
 * Guards against a return of the 12 px secondary <label> and the 10 px
 * (content-caption) label or help line measured in UX round 4 of #9880.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path: string) => readFileSync(join(SRC, path), "utf8");

const FILES = [
  "pages/NewAgent.tsx",
  "pages/NewKnowledgeBase.tsx",
  "pages/NewTemplate.tsx",
  "pages/custom-tool-detail/NewToolForm.tsx",
  "pages/mcp-server-detail/NewServerForm.tsx",
  "pages/prompt-detail/NewPromptForm.tsx",
  "pages/skill-detail/NewSkillForm.tsx",
  "pages/integration-detail/EmailSignatureCard.tsx",
];

describe("create form field labels", () => {
  it.each(FILES)("%s uses the design-system Label, no small raw <label>", (file) => {
    const source = read(file);
    expect(source).toMatch(/import \{ Label \} from "@\/components\/ui\/label";|import \{ SettingsField\b/);
    expect(source).not.toMatch(/<label\b/);
  });

  it.each([...FILES, "pages/skill-detail/PrivateSourceFields.tsx", "components/presentation/TemplateKindSelect.tsx"])(
    "%s has no 10 px or 11 px help line, error or label",
    (file) => {
      const source = read(file);
      expect(source).not.toMatch(/<(p|span|label|Label)[^>]*className="[^"]*content-caption/);
      expect(source).not.toMatch(/text-\[1[01]px\]/);
    },
  );

  it("the private source fields use the design-system Label on the create form", () => {
    const source = read("pages/skill-detail/PrivateSourceFields.tsx");
    expect(source).toMatch(/<Label htmlFor=\{htmlFor\} className="block text-default-primary">/);
  });

  it("the template kind trigger stays inside its column and shows the kind name only", () => {
    const source = read("components/presentation/TemplateKindSelect.tsx");
    expect(source).toMatch(/cn\("w-full min-w-0", className\)/);
    expect(source).toMatch(/<SelectValue>\s*<span className="block truncate">\{selectedLabel\}<\/span>/);
  });

  it.each([
    "pages/NewAgent.tsx",
    "pages/NewKnowledgeBase.tsx",
    "pages/NewTemplate.tsx",
    "pages/custom-tool-detail/NewToolForm.tsx",
    "pages/mcp-server-detail/NewServerForm.tsx",
    "pages/prompt-detail/NewPromptForm.tsx",
    "pages/skill-detail/NewSkillForm.tsx",
  ])("%s marks its required name field and gives it a help line (UX r1 B4 of #9926)", (file) => {
    const source = read(file);
    expect(source).toMatch(/<SettingsField[\s\S]{0,200}?\brequired\b[\s\S]{0,300}?helper=/);
  });

  it("names every New MCP server URL and OAuth field with a help line and an info bubble (UX r2 B1 of #9926)", () => {
    const source = read("pages/mcp-server-detail/NewServerForm.tsx");
    for (const id of ["url", "authorize-url", "token-url", "registration-url", "scopes", "client-id", "client-secret"]) {
      expect(source).toMatch(new RegExp(`<SettingsField\\s+id="new-server-${id}"[\\s\\S]{0,200}?helper=[\\s\\S]{0,400}?info=`));
      expect(source).toMatch(new RegExp(`id="new-server-${id}"[^>]{0,120}?aria-describedby=\\{settingsFieldHintId\\("new-server-${id}"\\)\\}`));
    }
    expect(source).toMatch(/<SettingsField\s+id="new-server-url"\s+required/);
  });

  it("names the code textareas by their label and ties their help line (UX r2 B2 of #9926)", () => {
    const prompt = read("pages/prompt-detail/NewPromptForm.tsx");
    expect(prompt).toMatch(/htmlFor="new-prompt-content"/);
    expect(prompt).toMatch(/id="new-prompt-content"\s+aria-describedby="new-prompt-content-hint"/);
    const tool = read("pages/custom-tool-detail/NewToolForm.tsx");
    expect(tool).toMatch(/htmlFor="new-tool-function"/);
    expect(tool).toMatch(/id="new-tool-function"\s+aria-describedby="new-tool-function-hint"/);
  });

  it("keeps the input parameter rows at the 36 px control height (UX r2 B3 of #9926)", () => {
    const tool = read("pages/custom-tool-detail/NewToolForm.tsx");
    expect(tool).not.toMatch(/<select\b/);
    expect(tool).not.toMatch(/className="h-8\b/);
    expect(tool).toMatch(/<SelectTrigger aria-label=\{t_i18n\("Type"\)\}/);
  });

  it.each([
    ["pages/NewAgent.tsx", ["new-agent-tags"]],
    ["pages/NewKnowledgeBase.tsx", ["new-kb-tags"]],
    ["pages/NewTemplate.tsx", ["new-template-tags"]],
    ["pages/custom-tool-detail/NewToolForm.tsx", ["new-tool-tags"]],
    ["pages/mcp-server-detail/NewServerForm.tsx", ["new-server-tags"]],
    ["pages/prompt-detail/NewPromptForm.tsx", ["new-prompt-tags"]],
    ["pages/skill-detail/NewSkillForm.tsx", ["new-skill-url-tags", "new-skill-paste-tags", "new-skill-upload-tags"]],
  ] as const)("%s gives its Tags typing box the label's id and ties its help line (UX r3 B1 of #9926)", (file, ids) => {
    const source = read(file);
    for (const id of ids) {
      expect(source).toMatch(new RegExp(`<TagInput id="${id}"[^>]{0,120}?aria-describedby=\\{settingsFieldHintId\\("${id}"\\)\\}`));
    }
    expect(source).not.toMatch(/<TagInput (?!id=)/);
  });

  it("names the New MCP server Client Secret by its label, not the generic fallback (UX r3 B2 of #9926)", () => {
    const source = read("pages/mcp-server-detail/NewServerForm.tsx");
    expect(source).toMatch(/<SecretField\s+id="new-server-client-secret"\s+aria-label=\{t_i18n\("Client Secret \(optional\)"\)\}/);
  });

  it("names the New MCP server stdio Command and Arguments by their label with a help line and an info bubble (UX r5 B1 of #9926)", () => {
    const source = read("pages/mcp-server-detail/NewServerForm.tsx");
    for (const id of ["command", "args"]) {
      expect(source).toMatch(new RegExp(`<SettingsField\\s+id="new-server-${id}"[\\s\\S]{0,200}?helper=[\\s\\S]{0,400}?info=`));
      expect(source).toMatch(new RegExp(`<Input id="new-server-${id}" aria-labelledby=\\{settingsFieldLabelId\\("new-server-${id}"\\)\\} aria-describedby=\\{settingsFieldHintId\\("new-server-${id}"\\)\\}`));
    }
    expect(source).not.toMatch(/aria-label=\{t_i18n\("(Command|Arguments \(space-separated\))"\)\}/);
  });

  it("ties the Email signature switch to its help line", () => {
    const source = read("pages/integration-detail/EmailSignatureCard.tsx");
    expect(source).toMatch(/aria-describedby="email-signature-enabled-help"/);
    expect(source).toMatch(/id="email-signature-enabled-help"/);
  });

  it("ties the Signature integration picker to its status and override lines (UX r4 B2 of #9926)", () => {
    const source = read("pages/integration-detail/EmailSignatureCard.tsx");
    expect(source).toMatch(/id="email-signature-integration"\s+aria-describedby="email-signature-integration-status email-signature-integration-override"/);
    expect(source).toMatch(/<p id="email-signature-integration-status"/);
    expect(source).toMatch(/<p id="email-signature-integration-override"/);
  });

  it("labels the private source picker and branch by id and ties their help lines (UX r4 B1 of #9926)", () => {
    const source = read("pages/skill-detail/PrivateSourceFields.tsx");
    expect(source).toMatch(/fieldLabel\(t_i18n\("Private repository access \(optional\)"\), githubIntegrations\.length > 0 \? accessId : undefined\)/);
    expect(source).toMatch(/<SearchableSelect\s+id=\{accessId\}\s+aria-describedby=\{accessHelpId\}/);
    expect(source).toMatch(/<p id=\{accessHelpId\}/);
    expect(source).toMatch(/<Input id=\{refId\} aria-describedby=\{refHelpId\}/);
    expect(source).toMatch(/<p id=\{refHelpId\}/);
  });
});
