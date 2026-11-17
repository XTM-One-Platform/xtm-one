// One focus ring geometry for text fields: the page-local textareas
// and inputs draw FIELD_FOCUS_RING (2 px ring-focus + 2 px offset, as the
// design-system Input), never the ring flush on the border.

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { FIELD_FOCUS_RING } from "./field-focus-ring";

// vitest runs from frontend/ (CI and `yarn test`), as ImageLightbox.test.tsx.
const SRC = join(process.cwd(), "src");

const FILES = [
  "pages/settings/ParametersSection.tsx",
  "pages/agent-detail/config/GeneralSection.tsx",
  "pages/flow-builder/FlowNode.tsx",
  // Flow builder inspector and dialogs (first slice).
  "pages/flow-builder/NodeConfigPanel.tsx",
  "pages/flow-builder/VariableInput.tsx",
  "pages/flow-builder/ExpandedFieldDialog.tsx",
  "pages/flow-builder/PrimitiveExtras.tsx",
  "pages/flow-builder/RunDialogs.tsx",
  // Authoring pages: prompts, skills, custom tools, templates, knowledge
  // base documents, assignment prompts and parameters (second slice).
  "pages/prompt-detail/ConfigTab.tsx",
  "pages/prompt-detail/NewPromptForm.tsx",
  "pages/skill-detail/CatalogTab.tsx",
  "pages/skill-detail/InstructionsTab.tsx",
  "pages/skill-detail/NewSkillForm.tsx",
  "pages/custom-tool-detail/ConfigTab.tsx",
  "pages/custom-tool-detail/NewToolForm.tsx",
  "pages/template-detail/ConfigTab.tsx",
  "pages/knowledge-base-detail/DocumentsTab.tsx",
  "pages/assignment-detail/PromptCard.tsx",
  "pages/assignment-detail/ParametersCard.tsx",
  // Evaluations and datasets (third slice).
  "pages/Datasets.tsx",
  "pages/DatasetDetail.tsx",
  "pages/NewEvaluation.tsx",
  "pages/EvaluationDetail.tsx",
  "components/evaluation/PlaceboConfigFields.tsx",
  "components/evaluation/evaluation-extractor-card.tsx",
  "components/evaluation/evaluation-settings-card.tsx",
];

/** Every `<textarea ...>` / `<input ...>` opening tag of a source file. */
function fieldTags(source: string): string[] {
  // Up to the self-closing `/>`: an arrow handler's `=>` sits inside the tag.
  // A tag name followed by whitespace: a comment's `<input>` is not a tag.
  return source.match(/<(?:textarea|input)\s[\s\S]*?\/>/g) ?? [];
}

/** Inputs that are not text fields take no text-field ring. */
const NOT_TEXT_FIELD = /\btype="(?:file|range|hidden|checkbox|radio|color)"/;

/**
 * Borderless fields that sit inside a bordered surface which carries the
 * focus cue, keyed by a fragment of the tag, with the reason.
 */
const RING_EXEMPT: Record<string, { fragment: string; reason: string }[]> = {};

/** The text field tags of a source file (textareas and text-like inputs). */
function textFieldTags(source: string): string[] {
  return fieldTags(source).filter((tag) => !NOT_TEXT_FIELD.test(tag));
}

describe("FIELD_FOCUS_RING", () => {
  it("draws the design-system Input geometry: 2 px ring-focus with a 2 px offset", () => {
    const classes = FIELD_FOCUS_RING.split(" ");
    expect(classes).toContain("focus-visible:ring-2");
    expect(classes).toContain("focus-visible:ring-focus");
    expect(classes).toContain("focus-visible:ring-offset-2");
    expect(classes).toContain("ring-offset-focus");
  });

  it.each(FILES)("%s: no hand-written field draws the ring flush on its border", (file) => {
    const source = readFileSync(join(SRC, file), "utf8");
    expect(source).toMatch(/FIELD_FOCUS_RING/);
    for (const tag of fieldTags(source)) {
      expect(tag).not.toMatch(/focus-visible:ring-focus\b/);
    }
  });

  // Banning the flush ring alone passes a field with no ring at all
  // (six DatasetDetail fields kept a 1.53:1 border cue).
  it.each(FILES)("%s: every hand-written text field draws FIELD_FOCUS_RING", (file) => {
    const source = readFileSync(join(SRC, file), "utf8");
    const exempt = RING_EXEMPT[file] ?? [];
    for (const tag of textFieldTags(source)) {
      if (exempt.some(({ fragment }) => tag.includes(fragment))) continue;
      expect(tag, tag.slice(0, 160)).toMatch(/FIELD_FOCUS_RING/);
    }
  });

  it("the flow canvas re-points the brand family at the XTM One accent in both modes", () => {
    const css = readFileSync(join(SRC, "index.css"), "utf8");
    const rule = css.match(
      /:where\(\[data-theme="xtm-one"\]\) \.react-flow:is\(\.dark, \.light\) \{([^}]*)\}/,
    );
    expect(rule).not.toBeNull();
    expect(rule?.[1]).toMatch(/--color-filigran-brand-primary: var\(--color-accent\);/);
  });

  it("the flow builder's chat test message placeholder goes through t_i18n", () => {
    const source = readFileSync(join(SRC, "pages/flow-builder/FlowNode.tsx"), "utf8");
    expect(source).not.toMatch(/Message de test/);
    expect(source).toMatch(/placeholder=\{t_i18n\("Test message…"\)\}/);
  });
});
