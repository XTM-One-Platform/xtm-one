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
 * What a message of the chat panel reads as on screen, in a copy and aloud
 * (`transformMessageText`): the panel's text, rewritten the way the platform
 * shows it.
 */
import { channelMarkdownForWeb, channelTextForWeb } from "../../lib/channel-text.ts";
import { prepareChatImages } from "../chat/file-image-src.ts";
import { showSecretMarkers } from "../../pages/chat/secretMarkers.ts";

/** What the page knows of a stored message beyond the panel's copy of it. */
interface StoredMessageFacts {
  /** The channel a user message was typed in (`slack`, `teams`), or the web. */
  channel?: string | null;
  /** How a reply was written (`mrkdwn` for one an agent wrote in Slack). */
  content_format?: string | null;
  /** The run a scheduled outcome reports on: drawn as the page's own action. */
  scheduled_outcome?: { run_id?: string | null } | null;
}

/**
 * The trailing link to its run a scheduled outcome is stored with, relative or absolute:
 * /operations/executions/:id since #797, /runs/:id in the messages stored before.
 */
const SCHEDULED_RUN_LINK = /\n*\[View the run\]\((?:https?:\/\/[^\s)]+)?\/(?:operations\/executions|runs)\/[^\s)]+\)\s*$/;

/**
 * A scheduled outcome without its trailing run link: the page draws the run as
 * its own action under the message (`ScheduledOutcomeAction`), every surface
 * outside the web app keeps the absolute link of the stored text (#11950).
 */
export function withoutScheduledRunLink(text: string): string {
  return text.replace(SCHEDULED_RUN_LINK, "");
}

/** Where a user message's own text ends and the text extracted from its files begins. */
const FILE_CONTEXT_SEPARATOR = "\n\n---\n\n";

/**
 * *text* as the page shows it: a stored secret as its display text, a message
 * typed in Slack or Teams or a reply written in Slack's mrkdwn as Markdown,
 * a user message with files without the text extracted from them, and a
 * reply's `file:<uuid>` images and links on the file's download path.
 */
export function messageTextForPage(
  text: string,
  role: "user" | "assistant",
  hasFiles: boolean,
  stored: StoredMessageFacts | undefined,
): string {
  let shown = text;
  if (role === "user") {
    if (hasFiles && shown.includes(FILE_CONTEXT_SEPARATOR)) shown = shown.split(FILE_CONTEXT_SEPARATOR)[0].trim();
    shown = channelTextForWeb(stored?.channel, shown);
  } else {
    shown = channelMarkdownForWeb(stored?.content_format, shown);
    if (stored?.scheduled_outcome?.run_id) shown = withoutScheduledRunLink(shown);
    // `![alt](file:<uuid>)` and `[text](file:<uuid>)` point at the file's
    // download path, and an API image's absolute address is cut to its path, so
    // the panel loads each with the reader's session.
    shown = prepareChatImages(shown);
  }
  return showSecretMarkers(shown);
}
