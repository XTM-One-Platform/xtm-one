/**
 * A chat panel message reads as XTM One writes it - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { messageTextForPage, withoutScheduledRunLink } from "./message-text.ts";

const SECRET = "[[SECRET_VARIABLE:11111111-1111-4111-8111-111111111111:0123456789abcdef]]";

test("a stored secret reads as its display text, in a reply and in a user message", () => {
  assert.equal(messageTextForPage(`The key is ${SECRET}.`, "assistant", false, undefined), "The key is [secret].");
  assert.equal(messageTextForPage(`Use ${SECRET}`, "user", false, undefined), "Use [secret]");
});

test("a user message with files shows the text typed, not the text extracted from them", () => {
  assert.equal(messageTextForPage("Summarize it\n\n---\n\nPage 1 of the PDF", "user", true, undefined), "Summarize it");
  assert.equal(messageTextForPage("a\n\n---\n\nb", "user", false, undefined), "a\n\n---\n\nb");
});

test("a message typed in Slack and a reply written in Slack's mrkdwn read as the web shows them", () => {
  assert.equal(messageTextForPage("see <https://x.test|the doc> &amp; <@U1|ana>", "user", false, { channel: "slack" }), "see the doc (https://x.test) & @ana");
  assert.equal(messageTextForPage("hi <at>Ana</at>", "user", false, { channel: "teams" }), "hi @Ana");
  assert.equal(messageTextForPage("*done* <https://x.test|here>", "assistant", false, { content_format: "slack_mrkdwn" }), "**done** [here](https://x.test)");
  assert.equal(messageTextForPage("*done*", "assistant", false, undefined), "*done*");
});

test("a reply's file: images and links read on the file's download path, a user message is left as typed", () => {
  const id = "0f8fad5b-d9cb-469f-a165-70867728950e";
  const path = `/api/v1/chat/files/${id}/download`;
  assert.equal(messageTextForPage(`![chart](file:${id})`, "assistant", false, undefined), `![chart](${path})`);
  assert.equal(messageTextForPage(`[the PDF](file:${id})`, "assistant", false, undefined), `[the PDF](${path})`);
  assert.equal(
    messageTextForPage(`![x](https://one.test/api/v1/chat/files/${id}/download)`, "assistant", false, undefined),
    `![x](${path})`,
  );
  assert.equal(messageTextForPage("[a](file:///tmp/a.txt)", "assistant", false, undefined), "[a](file:///tmp/a.txt)");
  assert.equal(messageTextForPage(`![chart](file:${id})`, "user", false, undefined), `![chart](file:${id})`);
});

test("a scheduled outcome shows without its run link, which the page draws as its own action (#11950)", () => {
  const outcome = { scheduled_outcome: { run_id: "r-1" } };
  const absolute = "**Done**: sent at 07:00.\n\n[View the run](https://one.test/runs/r-1)";
  const relative = "**Done**: sent at 07:00.\n\n[View the run](/runs/r-1)";
  assert.equal(messageTextForPage(absolute, "assistant", false, outcome), "**Done**: sent at 07:00.");
  assert.equal(messageTextForPage(relative, "assistant", false, outcome), "**Done**: sent at 07:00.");
  // A message without the run as data keeps its link: an older outcome reads as stored.
  assert.equal(messageTextForPage(relative, "assistant", false, undefined), relative);
  // Only the trailing run link goes: a link in the prose stays.
  assert.equal(withoutScheduledRunLink("See [View the run](/runs/a) later."), "See [View the run](/runs/a) later.");
});

test("a scheduled outcome linked to its run under Operations > Executions shows without that link (#797)", () => {
  const outcome = { scheduled_outcome: { run_id: "r-1" } };
  const absolute = "**Done**: sent at 07:00.\n\n[View the run](https://one.test/operations/executions/r-1)";
  const relative = "**Done**: sent at 07:00.\n\n[View the run](/operations/executions/r-1)";
  assert.equal(messageTextForPage(absolute, "assistant", false, outcome), "**Done**: sent at 07:00.");
  assert.equal(messageTextForPage(relative, "assistant", false, outcome), "**Done**: sent at 07:00.");
  assert.equal(withoutScheduledRunLink(relative), "**Done**: sent at 07:00.");
  assert.equal(
    withoutScheduledRunLink("See [View the run](/operations/executions/a) later."),
    "See [View the run](/operations/executions/a) later.",
  );
});
