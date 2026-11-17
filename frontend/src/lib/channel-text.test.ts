/**
 * Unit tests for how a message typed in Slack or Teams reads on the web -
 * `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { channelMarkdownForWeb, channelTextForWeb } from "./channel-text.ts";

const MRKDWN = "slack_mrkdwn";

test("a Slack message reads as the person saw it", () => {
  assert.equal(
    channelTextForWeb("slack", "see <https://example.com/a?b=1|the report> and <https://example.com>"),
    "see the report (https://example.com/a?b=1) and https://example.com",
  );
  assert.equal(channelTextForWeb("slack", "<https://x.test|https://x.test>"), "https://x.test");
  assert.equal(channelTextForWeb("slack", "ask <@U0123|ada> in <#C0456|security>"), "ask @ada in #security");
  assert.equal(channelTextForWeb("slack", "ping <@U0123> <!here>"), "ping @U0123 @here");
  assert.equal(channelTextForWeb("slack", "a &lt; b &amp;&amp; c &gt; d, &amp;lt;"), "a < b && c > d, &lt;");
});

test("a Teams mention reads as a mention", () => {
  assert.equal(channelTextForWeb("teams", "thanks <at>Ada Lovelace</at>!"), "thanks @Ada Lovelace!");
});

test("a web message is left as written", () => {
  const text = "keep <https://x|y> and <at>z</at> &amp;";
  assert.equal(channelTextForWeb("web", text), text);
  assert.equal(channelTextForWeb(undefined, text), text);
});

test("a reply written in Slack's mrkdwn reads as Markdown on the web", () => {
  assert.equal(
    channelMarkdownForWeb(MRKDWN, "*Contained* - see <https://status.test/a?b=1|the status page> or <https://status.test>"),
    "**Contained** - see [the status page](https://status.test/a?b=1) or [https://status.test](https://status.test)",
  );
  assert.equal(channelMarkdownForWeb(MRKDWN, "ask <@U0123|ada> in <#C0456|security> <!here>"), "ask @ada in #security @here");
  assert.equal(channelMarkdownForWeb(MRKDWN, "\u2022 first\n  \u2022 nested"), "- first\n  - nested");
  assert.equal(channelMarkdownForWeb(MRKDWN, "<https://x.test/a_(b)|wiki>"), "[wiki](https://x.test/a_%28b%29)");
  // _italic_, ~strike~ and > quotes already read the same in Markdown.
  assert.equal(channelMarkdownForWeb(MRKDWN, "_soon_ ~never~\n> quoted"), "_soon_ ~never~\n> quoted");
});

test("a Slack reply keeps its code, its markers and asterisks that are not bold", () => {
  const code = "`*not bold*` and\n```\n<https://x|y> *z*\n```";
  assert.equal(channelMarkdownForWeb(MRKDWN, code), code);
  const marker = "*Deck* [[PRESENTATION:0f7e2a10-0000-4000-8000-000000000001#/a_b]]";
  assert.equal(
    channelMarkdownForWeb(MRKDWN, marker),
    "**Deck** [[PRESENTATION:0f7e2a10-0000-4000-8000-000000000001#/a_b]]",
  );
  for (const text of ["**already bold**", "2 * 3 * 4", "a*b*c", "*", "* item"]) {
    assert.equal(channelMarkdownForWeb(MRKDWN, text), text, text);
  }
});

test("a Slack link keeps its destination, wildcards included", () => {
  assert.equal(
    channelMarkdownForWeb(MRKDWN, "<https://search.example/search?q=*incident*|Search> *now*"),
    "[Search](https://search.example/search?q=*incident*) **now**",
  );
  assert.equal(
    channelMarkdownForWeb(MRKDWN, "<https://search.example/?q=*a*_b_~c~>"),
    "[https://search.example/?q=\\*a\\*\\_b\\_\\~c\\~](https://search.example/?q=*a*_b_~c~)",
  );
  // The label is still the reply's text: its own markup is converted.
  assert.equal(channelMarkdownForWeb(MRKDWN, "<https://x.test/?q=*a*|*Read* it>"), "[**Read** it](https://x.test/?q=*a*)");
});

test("a reply not marked as mrkdwn is already Markdown, whatever channel it went to", () => {
  // The General Assistant answers in Slack in Markdown, converted only on its
  // way out: its *italics* stay italics on the web.
  const text = "The fix is *tentative*: **bold** <https://x|y> \u2022 z";
  assert.equal(channelMarkdownForWeb(null, text), text);
  assert.equal(channelMarkdownForWeb(undefined, text), text);
  assert.equal(channelMarkdownForWeb("markdown", text), text);
});
