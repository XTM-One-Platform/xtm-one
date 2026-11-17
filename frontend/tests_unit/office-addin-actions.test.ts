/**
 * The Office add-in's action block (`apps/microsoft-office/src/lib/actions.ts`):
 * an answer may end with a fenced `action` block naming what to do with the
 * document and the content to put there. The pane applies the block's
 * content on the user's click and never shows the block as text.
 *
 * Run with `yarn test:unit` (Node's built-in runner).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { extractAction, stripActionBlock } from "../../apps/microsoft-office/src/lib/actions.ts";

const block = (json: string) => "```action\n" + json + "\n```";

test("an answer's action block names the action and the content to apply", () => {
  const answer = `Here is the rewrite.\n\n${block('{"action":"replace_selection","content":"New text"}')}`;
  assert.deepEqual(extractAction(answer), { action: "replace_selection", content: "New text" });
});

test("a block that is not JSON, or lacks the action or its content, is no action", () => {
  assert.equal(extractAction(block("not json")), null);
  assert.equal(extractAction(block('{"content":"x"}')), null);
  assert.equal(extractAction(block('{"action":"insert_at_end"}')), null);
  assert.equal(extractAction(block('{"action":"insert_at_end","content":42}')), null);
  assert.equal(extractAction("No block at all."), null);
});

test("another fenced block is not an action block", () => {
  assert.equal(extractAction('```json\n{"action":"replace_selection","content":"x"}\n```'), null);
});

test("the shown answer drops the block and keeps the rest", () => {
  const answer = `Summary first.\n\n${block('{"action":"insert_at_end","content":"Appendix"}')}\n`;
  assert.equal(stripActionBlock(answer), "Summary first.");
  assert.equal(stripActionBlock("Plain answer."), "Plain answer.");
});
