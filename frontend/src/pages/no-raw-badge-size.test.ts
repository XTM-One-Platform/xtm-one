/**
 * Type tokens, not raw pixel sizes, on the Skills and Templates surfaces - `yarn test`.
 *
 * The status badges of /skills, the template cards of /templates and the agent
 * Skills card rendered at a raw `text-[9px]`, below the smallest type token
 * (`content-caption`) and under the 10 px floor for badges. They use the `content-caption` token now; this
 * guard keeps a raw size from coming back on those three files.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const SRC = join(process.cwd(), "src");

const FILES = [
  "pages/Skills.tsx",
  "pages/Templates.tsx",
  "pages/agent-detail/config/SkillsCard.tsx",
  // Shared badges rendered in the same card rows.
  "components/ui/builtin-badge.tsx",
  "components/ui/family-badge.tsx",
  "components/ui/needs-family-badge.tsx",
  "components/ui/company-managed-badge.tsx",
  "components/ui/tag-list.tsx",
];

test("Skills, Templates and the agent Skills card carry no text under 10px (text-[Npx], N < 10)", () => {
  for (const file of FILES) {
    const source = readFileSync(join(SRC, file), "utf8");
    assert.doesNotMatch(source, /text-\[\dpx\]/, `${file} carries a raw text size under 10px`);
  }
});

test("the inline explainer badge of the Skills card does not grow its line (py-0)", () => {
  const source = readFileSync(join(SRC, "pages/agent-detail/config/SkillsCard.tsx"), "utf8");
  assert.match(source, /className="content-caption leading-none px-1 py-0 align-middle">1 KB/);
});
