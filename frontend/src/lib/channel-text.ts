/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * A message written in Slack or Teams, as the web shows it.
 *
 * A conversation continued across channels shows on the web what was typed in
 * Slack or Teams, stored as the platform delivered it: Slack writes links,
 * mentions and its three special characters in its own markup
 * (`<https://x|label>`, `<@U123>`, `&amp;`), Teams wraps a mention in
 * `<at>...</at>`. The web renders a user message as plain text, so that markup
 * is turned into what the person saw; a reply an agent wrote in Slack's
 * mrkdwn, into the Markdown the web renders it with. Pure, so
 * `yarn test:unit` covers it.
 */

const SLACK_LINK = /<((?:https?|mailto):[^|>\s]+)\|([^>]+)>/g;
const SLACK_BARE_LINK = /<((?:https?|mailto):[^>\s]+)>/g;
const SLACK_USER = /<@([A-Z0-9]+)(?:\|([^>]+))?>/g;
const SLACK_CHANNEL = /<#([A-Z0-9]+)(?:\|([^>]*))?>/g;
const SLACK_BROADCAST = /<!(here|channel|everyone)(?:\|[^>]*)?>/g;
const TEAMS_MENTION = /<at>([\s\S]*?)<\/at>/g;

function slackText(text: string): string {
  return (
    text
      .replace(SLACK_LINK, (_m, url: string, label: string) => (label === url ? url : `${label} (${url})`))
      .replace(SLACK_BARE_LINK, "$1")
      .replace(SLACK_USER, (_m, id: string, name?: string) => `@${name || id}`)
      .replace(SLACK_CHANNEL, (_m, id: string, name?: string) => `#${name || id}`)
      .replace(SLACK_BROADCAST, "@$1")
      // Slack escapes exactly these three; `&amp;` last, so `&amp;lt;` stays `&lt;`.
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&")
  );
}

/** *text*, written on *channel*, as the web shows it. */
export function channelTextForWeb(channel: string | null | undefined, text: string): string {
  if (channel === "slack") return slackText(text);
  if (channel === "teams") return text.replace(TEAMS_MENTION, "@$1");
  return text;
}

// An agent's reply to a Slack message is written in Slack's mrkdwn
// (`output_format.CHANNEL_FORMAT_OVERRIDES`), stored as written and marked
// `content_format: "slack_mrkdwn"`. Other replies sent to Slack (the General
// Assistant's, the bot's own notices) are Markdown. Code and the reply's
// `[[FILE:...]]`-style markers are left as they are.
const CODE = /```[\s\S]*?```|`[^`\n]+`/g;
const MARKER = /\[\[[^\]\n]+\]\]/g;
const SLACK_BOLD = /(^|[^\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])/g;
const SLACK_BULLET = /^(\s*)\u2022\s+/gm;
// Private Use Area characters: no reply carries them.
const KEPT = /\uE000(\d+)\uE001/g;

function slackMarkdown(text: string): string {
  const kept: string[] = [];
  const keep = (match: string) => `\uE000${kept.push(match) - 1}\uE001`;
  // A destination is kept out of the formatting below: `?q=*x*` stays the query it is.
  const href = (url: string) => keep(url.replace(/\(/g, "%28").replace(/\)/g, "%29"));
  // A bare link's text is the URL itself, shown as written rather than as emphasis.
  const literal = (url: string) => keep(url.replace(/[*_~]/g, "\\$&"));
  return text
    .replace(CODE, keep)
    .replace(MARKER, keep)
    .replace(SLACK_LINK, (_m, url: string, label: string) => `[${label}](${href(url)})`)
    .replace(SLACK_BARE_LINK, (_m, url: string) => `[${literal(url)}](${href(url)})`)
    .replace(SLACK_USER, (_m, id: string, name?: string) => `@${name || id}`)
    .replace(SLACK_CHANNEL, (_m, id: string, name?: string) => `#${name || id}`)
    .replace(SLACK_BROADCAST, "@$1")
    .replace(SLACK_BOLD, "$1**$2**")
    .replace(SLACK_BULLET, "$1- ")
    .replace(KEPT, (_m, index: string) => kept[Number(index)]);
}

/** A reply stored in *contentFormat* (`MessageResponse.content_format`), as the Markdown the web renders. */
export function channelMarkdownForWeb(contentFormat: string | null | undefined, text: string): string {
  return contentFormat === "slack_mrkdwn" ? slackMarkdown(text) : text;
}
