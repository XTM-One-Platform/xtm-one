/**
 * The template form's chrome validation counts the right footer as a pattern,
 * as the backend `validate_template_chrome` does: a variable read only
 * by `footer_right` is used, `{{page}}` and `{{year}}` need no declaration
 * there, and a right footer alone still fills declared variables.
 */
import { describe, expect, it } from "vitest";

import { type TemplateChromeDraft, validateTemplateChrome } from "./chrome-pattern";

const CLIENT = { key: "client", label: "Client" };

function draft(overrides: Partial<TemplateChromeDraft>): TemplateChromeDraft {
  return { footer: "", header: "", variables: [], ...overrides };
}

describe("validateTemplateChrome with a right footer", () => {
  it("counts a variable read only by footer_right as used", () => {
    const { canonical, problems } = validateTemplateChrome(
      draft({
        footer: "{{title}}",
        footer_right: "{{page}} \u00b7 \u00a9 {{year}} {{client}}",
        variables: [CLIENT],
      }),
    );
    expect(problems).toEqual([]);
    expect(canonical).toMatchObject({
      footer: "{{title}}",
      header: null,
      variables: [CLIENT],
      footer_right: "{{page}} \u00b7 \u00a9 {{year}} {{client}}",
    });
  });

  it("treats a right footer alone as a pattern for declared variables", () => {
    const { canonical, problems } = validateTemplateChrome(
      draft({ footer_right: "\u00a9 {{year}} {{client}}", variables: [CLIENT] }),
    );
    expect(problems).toEqual([]);
    expect(canonical).toMatchObject({ footer: null, header: null, variables: [CLIENT] });
  });

  it("still reports an undeclared variable in footer_right", () => {
    const { canonical, problems } = validateTemplateChrome(
      draft({ footer: "{{title}}", footer_right: "{{page}} {{client}}" }),
    );
    expect(canonical).toBeNull();
    expect(problems).toEqual([{ code: "placeholder_undeclared", detail: "{{client}}" }]);
  });

  it("keeps a variable no pattern reads reported as unused", () => {
    const { problems } = validateTemplateChrome(
      draft({ footer: "{{title}}", footer_right: "{{page}}", variables: [CLIENT] }),
    );
    expect(problems).toEqual([{ code: "variable_unused", detail: "{{client}}" }]);
  });

  it("keeps page and year undeclared in the footer, where they are not filled", () => {
    const { problems } = validateTemplateChrome(draft({ footer: "{{title}} {{page}}" }));
    expect(problems).toEqual([{ code: "placeholder_undeclared", detail: "{{page}}" }]);
  });
});
