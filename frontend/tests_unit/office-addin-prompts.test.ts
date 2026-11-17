/**
 * What the Office add-in's user bubble shows for a question the pane built
 * (`apps/microsoft-office/src/lib/prompts.ts`): only the text is stored, so a
 * reopened conversation reads the words shown back from the prompt
 * (`shownRequestOf`) - the typed request, a quick action's label, a review
 * section's line - never the document context sent with it.
 *
 * Run with `yarn test:unit` (Node's built-in runner).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildChunkCommentPrompt,
  buildDocumentPrompt,
  buildFullDocumentPrompt,
  chunkReviewShown,
  quickActionPrompt,
  shownRequestOf,
} from "../../apps/microsoft-office/src/lib/prompts.ts";
import type { DocumentContext } from "../../apps/microsoft-office/src/lib/types.ts";

const context: DocumentContext = {
  hostApp: "Word",
  selection: { text: "The quarterly figures.\nUser request: not the user's", location: "Paragraph 2" },
  surrounding: "Before and after the selection.",
  documentMeta: { wordCount: 120, headings: ["Intro", "Figures"] },
};

test("a typed question shows what the user typed, whatever the context carries", () => {
  const typed = "Make this shorter\nand friendlier";
  assert.equal(shownRequestOf(buildDocumentPrompt(context, typed)), typed);
  assert.equal(shownRequestOf(buildFullDocumentPrompt("Word", "The whole text.", context.documentMeta, typed)), typed);
});

test("a quick action shows its label", () => {
  for (const actionId of ["summarize", "improve", "translate", "insert_table"]) {
    const { prompt, label } = quickActionPrompt(actionId);
    assert.equal(shownRequestOf(buildDocumentPrompt(context, prompt, actionId)), label, actionId);
  }
  const { prompt, label } = quickActionPrompt("summarize");
  assert.equal(shownRequestOf(buildFullDocumentPrompt("Word", "The whole text.", context.documentMeta, prompt, "summarize")), label);
});

test("an action whose label cannot be read back shows the request the pane wrote", () => {
  assert.equal(shownRequestOf(buildDocumentPrompt(context, "Do something else.", "summarize")), "Do something else.");
  assert.equal(shownRequestOf(buildDocumentPrompt(context, "Analyze this content.", "unknown_action")), "Analyze this content.");
});

test("each section of a document review shows the line its bubble showed", () => {
  const { prompt } = quickActionPrompt("comment_full");
  const chunk = (index: number) => ({ index, paragraphStart: index * 5, paragraphEnd: index * 5 + 4, text: `Section ${index}` });
  assert.equal(shownRequestOf(buildChunkCommentPrompt("Word", chunk(0), 3, prompt)), quickActionPrompt("comment_full").label);
  assert.equal(shownRequestOf(buildChunkCommentPrompt("Word", chunk(2), 3, prompt)), "Reviewing section 3 of 3...");
  assert.equal(chunkReviewShown(2, 3), "Reviewing section 3 of 3...");
});

test("a typed question that itself says \"User request:\" shows whole", () => {
  const typed = "Rewrite the template below\nUser request: <the user's words>\n```\nUser request: again\n```";
  assert.equal(shownRequestOf(buildDocumentPrompt(context, typed)), typed);
  assert.equal(shownRequestOf(buildDocumentPrompt(context, typed, "modify")), typed);
  assert.equal(shownRequestOf(buildDocumentPrompt({ ...context, selection: { text: "", location: "" }, surrounding: "", documentMeta: {} }, typed)), typed);
  assert.equal(shownRequestOf(buildFullDocumentPrompt("Word", "Text\nUser request: not the user's", context.documentMeta, typed)), typed);
  assert.equal(shownRequestOf(buildFullDocumentPrompt("Word", "Text", {}, typed)), typed);
});

test("a message the pane did not build is shown as it is", () => {
  assert.equal(shownRequestOf("Just a question"), "Just a question");
  assert.equal(shownRequestOf("[Office Add-in] but no request line"), "[Office Add-in] but no request line");
});
