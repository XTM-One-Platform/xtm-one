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
 * The messages the presentation UI hands to the user's assistant.
 *
 * The chat composer is primed with these (see `lib/chat-launch.ts`); nothing is
 * sent until the user presses send, so the wording is written to be read and
 * completed by a person, not parsed by code.
 *
 * Editing an EXISTING deck no longer goes through a primed message: "Edit with
 * my assistant" opens the Presentation Studio (`studio-url.ts`), where the
 * conversation is bound to the deck and the agent is told about it on every
 * turn — no message has to name the deck by id for the agent to find it.
 *
 * Creating a NEW deck settles its DESIGN first: the message names the
 * template the user picked (by name — the agent resolves it with
 * `search_templates`, the same way it resolves a template a person types) and
 * the colour scheme, or says "without a template". A message that names
 * neither leaves the choice to the agent, which asks when the user has several
 * templates to pick from and otherwise uses the only one there is.
 */

import type { PresentationTheme } from "@/types";

interface CreateWithAssistantOptions {
  /**
   * The design the deck starts from: a template by name, `null` for no
   * template (the plain look), `undefined` to let the assistant ask.
   */
  template?: { name: string } | null;
  /** The colour scheme; omitted = the template's own default. */
  theme?: PresentationTheme;
  /** What the deck is about; omitted = the user finishes the sentence in the chat. */
  topic?: string;
}

const THEME_WORDS: Record<PresentationTheme, string> = { dark: "dark", light: "light" };

/**
 * Library empty state / "New presentation": the user finishes the sentence
 * (the topic goes LAST, so an empty topic leaves the cursor where it belongs).
 */
export function createWithAssistantPrompt(options: CreateWithAssistantOptions = {}): string {
  const topic = options.topic?.trim() ?? "";
  const about = `create a presentation about ${topic}`;
  const theme = options.theme ? THEME_WORDS[options.theme] : null;
  if (options.template === null) {
    const look = theme ? `plain ${theme} look` : "plain look";
    return `Without a template (${look}), ${about}`;
  }
  if (options.template) {
    const scheme = theme ? ` (${theme} theme)` : "";
    return `Using the "${options.template.name}" template${scheme}, ${about}`;
  }
  return `Create a presentation about ${topic}`;
}
