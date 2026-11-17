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

import type { DocumentChunk, DocumentContext, OfficeHostApp } from "./types";

const TOKEN_BUDGET = 6000;
const FULL_DOC_BUDGET = 12000;

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max) + "\n… [truncated]";
}

function appLabel(hostApp: OfficeHostApp): string {
  switch (hostApp) {
    case "Word":
      return "Microsoft Word document";
    case "Excel":
      return "Microsoft Excel spreadsheet";
    case "PowerPoint":
      return "Microsoft PowerPoint presentation";
    case "Outlook":
      return "Microsoft Outlook email";
    default:
      return "document";
  }
}

// ---------------------------------------------------------------------------
// Tool capabilities preamble — tells the agent what it can do
// ---------------------------------------------------------------------------

function toolCapabilities(hostApp: OfficeHostApp): string {
  const common =
    "You are assisting the user through a Microsoft Office Add-in. " +
    "The user will apply your suggestions to the document using action buttons (Replace, Insert below, Comment). " +
    "Keep responses concise and actionable. When rewriting or modifying content, return ONLY the new text without explanations.";

  switch (hostApp) {
    case "Word":
      return (
        common +
        " Available actions: replace selected text, insert text at the end, add comments on text, insert HTML, insert tables. " +
        "The user can select text in Word and you will see it in the context."
      );
    case "Excel":
      return (
        common +
        " Available actions: write values to cells, write formulas, add cell comments. " +
        "The user can select a cell range and you will see its values."
      );
    case "PowerPoint":
      return (
        common +
        " Available actions: replace text in shapes, add new slides with text, add text boxes. " +
        "The user can select text on a slide and you will see it."
      );
    case "Outlook":
      return (
        common +
        " Available actions: replace email body, insert text at cursor. " +
        "You can see the email subject, sender, recipients, and body text. " +
        "Help the user draft replies, summarize emails, translate, or improve their writing."
      );
    default:
      return common;
  }
}

// ---------------------------------------------------------------------------
// Prompt builder – three-tier context injection
//   Tier 1: selection (always included)
//   Tier 2: surrounding paragraph / range (if available)
//   Tier 3: document meta (headings, sheet names, counts)
// ---------------------------------------------------------------------------

export function buildDocumentPrompt(
  context: DocumentContext,
  userMessage: string,
  action?: string,
): string {
  const parts: string[] = [];
  let budget = TOKEN_BUDGET;

  parts.push(
    `[Office Add-in] ${toolCapabilities(context.hostApp)}`,
  );

  // Tier 1 – selection
  if (context.selection.text) {
    const selChunk = truncate(context.selection.text, Math.floor(budget * 0.5));
    budget -= selChunk.length;
    parts.push(
      `\nSelected content (${context.selection.location}):\n\`\`\`\n${selChunk}\n\`\`\``,
    );
  }

  // Tier 2 – surrounding context
  if (context.surrounding && budget > 200) {
    const surChunk = truncate(context.surrounding, Math.min(budget, 2000));
    budget -= surChunk.length;
    parts.push(`\nSurrounding context:\n\`\`\`\n${surChunk}\n\`\`\``);
  }

  // Tier 3 – document metadata
  const meta = context.documentMeta;
  const metaParts: string[] = [];

  if (meta.wordCount !== undefined) metaParts.push(`Words: ${meta.wordCount}`);
  if (meta.pageCount !== undefined) metaParts.push(`Pages: ${meta.pageCount}`);
  if (meta.slideCount !== undefined)
    metaParts.push(`Slides: ${meta.slideCount}`);
  if (meta.tableCount !== undefined)
    metaParts.push(`Tables: ${meta.tableCount}`);
  if (meta.usedRangeSize) metaParts.push(`Used range: ${meta.usedRangeSize}`);
  if (meta.sheetNames?.length)
    metaParts.push(`Sheets: ${meta.sheetNames.join(", ")}`);
  if (meta.headings?.length) {
    const headingStr = meta.headings.slice(0, 15).join(" | ");
    metaParts.push(`Headings: ${headingStr}`);
  }

  if (metaParts.length) {
    parts.push(`\nDocument structure: ${metaParts.join(" · ")}`);
  }

  // Action instruction
  if (action) {
    const desc = ACTION_DESCRIPTIONS[action] || action;
    parts.push(`\nRequested action: ${desc}`);
  }

  parts.push(`\nUser request: ${userMessage}`);

  return parts.join("\n");
}

// ---------------------------------------------------------------------------
// Full-document prompt – sends all content for summarize / full-doc actions
// ---------------------------------------------------------------------------

export function buildFullDocumentPrompt(
  hostApp: OfficeHostApp,
  fullText: string,
  meta: DocumentContext["documentMeta"],
  userMessage: string,
  action?: string,
): string {
  const parts: string[] = [];

  parts.push(
    `[Office Add-in] ${toolCapabilities(hostApp)}`,
  );

  const metaParts: string[] = [];
  if (meta.wordCount !== undefined) metaParts.push(`Words: ${meta.wordCount}`);
  if (meta.headings?.length)
    metaParts.push(`Headings: ${meta.headings.slice(0, 20).join(" | ")}`);
  if (meta.tableCount !== undefined)
    metaParts.push(`Tables: ${meta.tableCount}`);
  if (metaParts.length)
    parts.push(`\nDocument structure: ${metaParts.join(" · ")}`);

  parts.push(
    `\nFull document content:\n\`\`\`\n${truncate(fullText, FULL_DOC_BUDGET)}\n\`\`\``,
  );

  if (action) {
    const desc = ACTION_DESCRIPTIONS[action] || action;
    parts.push(`\nRequested action: ${desc}`);
  }

  parts.push(`\nUser request: ${userMessage}`);

  return parts.join("\n");
}

// ---------------------------------------------------------------------------
// Chunk-level prompt – for commenting/reviewing a section of the document
// ---------------------------------------------------------------------------

const CHUNK_INSTRUCTION = "Provide a brief, specific review comment for this section. Return ONLY the comment text, nothing else.";

export function buildChunkCommentPrompt(
  hostApp: OfficeHostApp,
  chunk: DocumentChunk,
  totalChunks: number,
  userMessage: string,
): string {
  return [
    `[Office Add-in Context] The user is working in a ${appLabel(hostApp)}.`,
    `\nReviewing section ${chunk.index + 1} of ${totalChunks} (paragraphs ${chunk.paragraphStart + 1}–${chunk.paragraphEnd + 1}):`,
    `\`\`\`\n${truncate(chunk.text, FULL_DOC_BUDGET)}\n\`\`\``,
    `\n${CHUNK_INSTRUCTION}`,
    `\nUser request: ${userMessage}`,
  ].join("\n");
}

/** What the user's bubble shows for section *index* (0-based) of a document review of *total* sections. */
export function chunkReviewShown(index: number, total: number): string {
  return index === 0 ? quickActionPrompt("comment_full").label : `Reviewing section ${index + 1} of ${total}...`;
}

const REQUEST_MARKER = "\nUser request: ";
const REQUESTED_ACTION = /\nRequested action: ([^\n]*)\n$/;
const CHUNK_SECTION = /^\[Office Add-in Context\][^\n]*\n\nReviewing section (\d+) of (\d+) \(/;
/** What a builder writes right before the request: its header line, a closing fence, the structure or the action. */
const BEFORE_REQUEST = /(?:^\[Office Add-in[^\n]*|\n```|\n\nDocument structure: [^\n]*|\n\nRequested action: [^\n]*)\n$/;

/**
 * Where the request starts: the first marker right after one of the builder's
 * own sections, so a marker in the document's text or in the request itself
 * is not taken for it.
 */
function requestMarkerAt(text: string): number {
  for (let at = text.indexOf(REQUEST_MARKER); at >= 0; at = text.indexOf(REQUEST_MARKER, at + 1)) {
    const before = text.slice(0, at);
    if (BEFORE_REQUEST.test(before) || before.endsWith(`\n\n${CHUNK_INSTRUCTION}\n`)) return at;
  }
  // No marker follows a section as the builders write it (a heading on two lines): the last one.
  return text.lastIndexOf(REQUEST_MARKER);
}

/**
 * What the user's bubble showed for a question the pane built, read back from
 * the stored text (only the text is stored, not the words shown): the user's
 * request, a quick action's label, a review section's line. Any other text is
 * returned as it is.
 */
export function shownRequestOf(text: string): string {
  if (!text.startsWith("[Office Add-in")) return text;
  const at = requestMarkerAt(text);
  if (at < 0) return text;
  const request = text.slice(at + REQUEST_MARKER.length);
  const before = text.slice(0, at);
  const section = CHUNK_SECTION.exec(before);
  if (section) return chunkReviewShown(Number(section[1]) - 1, Number(section[2]));
  // Only a quick action names its action; its label is what was shown.
  const requested = REQUESTED_ACTION.exec(before);
  if (requested) {
    const actionId = Object.keys(QUICK_ACTION_PROMPTS).find(
      (id) => (ACTION_DESCRIPTIONS[id] || id) === requested[1] && QUICK_ACTION_PROMPTS[id].prompt === request,
    );
    if (actionId) return QUICK_ACTION_PROMPTS[actionId].label;
  }
  return request;
}

/** Whether a typed question is about the whole document, which is then sent in full (Word). */
export function isFullDocRequest(text: string): boolean {
  const lower = text.toLowerCase();
  return (
    /\b(full|entire|whole|complete)\s+(document|doc|file|text)\b/.test(lower) ||
    /\bsummar(y|ize|ise)\b/.test(lower) ||
    /\b(comment|review)\s+(on\s+)?(the\s+)?(full|entire|whole|this)\s+(document|doc)\b/.test(lower)
  );
}

/** What a quick action asks the agent, and the words the user's bubble shows for it. */
const QUICK_ACTION_PROMPTS: Record<string, { prompt: string; label: string }> = {
  interpret: { prompt: "Analyze and interpret the document content.", label: "Interpret this document" },
  summarize: { prompt: "Summarize this document concisely.", label: "Summarize this document" },
  translate: { prompt: "Translate the selected text.", label: "Translate the selected text" },
  modify: { prompt: "Improve and rewrite the selected content.", label: "Rewrite the selected content" },
  improve: { prompt: "Enhance the writing quality.", label: "Improve the writing quality" },
  comment: { prompt: "Provide a review comment on this content.", label: "Add a review comment" },
  comment_full: {
    prompt: "Review the full document and provide comments section by section.",
    label: "Review the full document",
  },
  formula: { prompt: "Help with formulas for this data.", label: "Help with formulas" },
  insert_table: { prompt: "Generate a table from this content.", label: "Generate a table" },
  insert_html: { prompt: "Generate rich formatted content.", label: "Generate formatted content" },
};

export function quickActionPrompt(actionId: string): { prompt: string; label: string } {
  return QUICK_ACTION_PROMPTS[actionId] ?? { prompt: "Analyze this content.", label: "Analyze this content." };
}

export const FULL_DOC_ACTIONS = new Set([
  "summarize",
  "interpret",
  "comment_full",
]);

// ---------------------------------------------------------------------------
// Action descriptions
// ---------------------------------------------------------------------------

const ACTION_DESCRIPTIONS: Record<string, string> = {
  interpret:
    "Analyze and interpret the document content. Provide insights, summaries, or explanations.",
  modify:
    "Suggest modifications to the selected content. Return ONLY the modified text that should replace the selection.",
  comment:
    "Provide a review comment on the selected content. Return ONLY the comment text.",
  comment_full:
    "Review this section of the document and provide a specific, actionable comment. Return ONLY the comment text.",
  summarize: "Summarize the document content concisely.",
  translate:
    "Translate the selected content. Return ONLY the translated text.",
  improve:
    "Improve the writing quality of the selected content. Return ONLY the improved text.",
  formula:
    "Help with Excel formulas. Provide the formula or explain the data.",
  insert_table:
    "Generate a table from the content. Return an action block with action 'insert_table' and HTML table markup in 'content'.",
  insert_html:
    "Generate rich HTML content. Return an action block with action 'insert_html' and the HTML in 'content'.",
};

// ---------------------------------------------------------------------------
// Available actions per host app
// ---------------------------------------------------------------------------

export function getAvailableActions(hostApp: OfficeHostApp): {
  id: string;
  label: string;
  icon: string;
  description: string;
}[] {
  const common = [
    {
      id: "interpret",
      label: "Interpret",
      icon: "Lightbulb",
      description: "Analyze and explain content",
    },
    {
      id: "summarize",
      label: "Summarize",
      icon: "FileText",
      description: "Create a concise summary",
    },
    {
      id: "translate",
      label: "Translate",
      icon: "Languages",
      description: "Translate selected text",
    },
  ];

  switch (hostApp) {
    case "Word":
      return [
        ...common,
        {
          id: "modify",
          label: "Modify",
          icon: "Pencil",
          description: "Rewrite selected text",
        },
        {
          id: "improve",
          label: "Improve",
          icon: "Sparkles",
          description: "Enhance writing quality",
        },
        {
          id: "comment",
          label: "Comment",
          icon: "MessageSquare",
          description: "Comment on selection",
        },
        {
          id: "comment_full",
          label: "Review Doc",
          icon: "FileText",
          description: "Comment the full document section by section",
        },
        {
          id: "insert_table",
          label: "Insert Table",
          icon: "Table",
          description: "Generate and insert a table",
        },
        {
          id: "insert_html",
          label: "Insert HTML",
          icon: "Code",
          description: "Insert rich formatted content",
        },
      ];

    case "Excel":
      return [
        ...common,
        {
          id: "modify",
          label: "Modify",
          icon: "Pencil",
          description: "Transform selected data",
        },
        {
          id: "formula",
          label: "Formula",
          icon: "Calculator",
          description: "Help with formulas",
        },
        {
          id: "comment",
          label: "Comment",
          icon: "MessageSquare",
          description: "Add a cell comment",
        },
        {
          id: "insert_table",
          label: "Insert Table",
          icon: "Table",
          description: "Generate a data table",
        },
      ];

    case "PowerPoint":
      return [
        ...common,
        {
          id: "modify",
          label: "Modify",
          icon: "Pencil",
          description: "Rewrite selected text",
        },
        {
          id: "improve",
          label: "Improve",
          icon: "Sparkles",
          description: "Enhance slide content",
        },
        {
          id: "insert_html",
          label: "Insert HTML",
          icon: "Code",
          description: "Insert formatted content",
        },
      ];

    case "Outlook":
      return [
        {
          id: "summarize",
          label: "Summarize",
          icon: "FileText",
          description: "Summarize this email",
        },
        {
          id: "interpret",
          label: "Interpret",
          icon: "Lightbulb",
          description: "Analyze the email content",
        },
        {
          id: "modify",
          label: "Draft Reply",
          icon: "Pencil",
          description: "Draft a reply to this email",
        },
        {
          id: "improve",
          label: "Improve",
          icon: "Sparkles",
          description: "Improve your draft",
        },
        {
          id: "translate",
          label: "Translate",
          icon: "Languages",
          description: "Translate the email",
        },
      ];

    default:
      return common;
  }
}
