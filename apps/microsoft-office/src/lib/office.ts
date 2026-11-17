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

import type { DocumentChunk, DocumentContext, DocumentMeta, OfficeHostApp, SelectionInfo } from "./types";

export function getHostApp(): OfficeHostApp {
  if (!window.Office?.context?.host) return "Unknown";
  const host = Office.context.host;
  if (host === Office.HostType.Word) return "Word";
  if (host === Office.HostType.Excel) return "Excel";
  if (host === Office.HostType.PowerPoint) return "PowerPoint";
  if (host === Office.HostType.Outlook) return "Outlook";
  return "Unknown";
}

// ---------------------------------------------------------------------------
// Word
// ---------------------------------------------------------------------------

async function wordGetSelection(): Promise<SelectionInfo> {
  return Word.run(async (ctx: Word.RequestContext) => {
    const sel = ctx.document.getSelection();
    sel.load("text");
    const paragraphs = sel.paragraphs;
    paragraphs.load("items");
    await ctx.sync();

    const idx = paragraphs.items.length > 0 ? 1 : 0;
    return {
      text: sel.text || "",
      location: idx > 0 ? `Paragraph ${idx}` : "Document",
    };
  });
}

async function wordGetSurrounding(): Promise<string> {
  return Word.run(async (ctx: Word.RequestContext) => {
    const body = ctx.document.body;
    const paragraphs = body.paragraphs;
    paragraphs.load("items/text");
    await ctx.sync();

    const texts: string[] = [];
    let charCount = 0;
    const MAX_CHARS = 4000;
    for (const p of paragraphs.items) {
      const t = p.text?.trim();
      if (!t) continue;
      if (charCount + t.length > MAX_CHARS) {
        texts.push(t.slice(0, MAX_CHARS - charCount) + "…");
        break;
      }
      texts.push(t);
      charCount += t.length;
    }
    return texts.join("\n\n");
  });
}

async function wordGetMeta(): Promise<DocumentMeta> {
  return Word.run(async (ctx: Word.RequestContext) => {
    const body = ctx.document.body;
    body.load("text");
    const paragraphs = body.paragraphs;
    paragraphs.load("items/style,items/text");
    await ctx.sync();

    const words = (body.text || "").split(/\s+/).filter(Boolean).length;
    const headings: string[] = [];
    let tableCount = 0;
    for (const p of paragraphs.items) {
      if (p.style && /heading/i.test(p.style) && p.text?.trim()) {
        headings.push(p.text.trim());
      }
    }

    try {
      const tables = body.tables;
      tables.load("items");
      await ctx.sync();
      tableCount = tables.items.length;
    } catch {
      // tables API may not be available
    }

    return { wordCount: words, headings, tableCount };
  });
}

export async function wordGetFullText(): Promise<string> {
  return Word.run(async (ctx: Word.RequestContext) => {
    const body = ctx.document.body;
    body.load("text");
    await ctx.sync();
    return body.text || "";
  });
}

export async function wordGetDocumentChunks(
  chunkSize = 3000,
): Promise<DocumentChunk[]> {
  return Word.run(async (ctx: Word.RequestContext) => {
    const paragraphs = ctx.document.body.paragraphs;
    paragraphs.load("items/text");
    await ctx.sync();

    const chunks: DocumentChunk[] = [];
    let current = "";
    let startIdx = 0;

    for (let i = 0; i < paragraphs.items.length; i++) {
      const t = paragraphs.items[i].text?.trim() || "";
      if (!t) continue;

      if (current.length + t.length > chunkSize && current.length > 0) {
        chunks.push({
          index: chunks.length,
          text: current,
          paragraphStart: startIdx,
          paragraphEnd: i - 1,
        });
        current = t;
        startIdx = i;
      } else {
        current += (current ? "\n\n" : "") + t;
      }
    }

    if (current) {
      chunks.push({
        index: chunks.length,
        text: current,
        paragraphStart: startIdx,
        paragraphEnd: paragraphs.items.length - 1,
      });
    }

    return chunks;
  });
}

async function wordInsertText(
  text: string,
  location: "Replace" | "End" | "Start" = "End",
): Promise<void> {
  await Word.run(async (ctx: Word.RequestContext) => {
    const sel = ctx.document.getSelection();
    sel.insertText(text, location);
    await ctx.sync();
  });
}

async function wordReplaceSelection(text: string): Promise<void> {
  await wordInsertText(text, "Replace");
}

async function wordInsertAtEnd(text: string): Promise<void> {
  await Word.run(async (ctx: Word.RequestContext) => {
    const body = ctx.document.body;
    body.insertText(text, "End");
    await ctx.sync();
  });
}

async function wordAddComment(text: string): Promise<void> {
  await Word.run(async (ctx: Word.RequestContext) => {
    const sel = ctx.document.getSelection();
    sel.load("text");
    await ctx.sync();

    if (sel.text) {
      sel.insertComment(text);
      await ctx.sync();
      return;
    }

    const firstPara = ctx.document.body.paragraphs.getFirst();
    const range = firstPara.getRange();
    range.insertComment(text);
    await ctx.sync();
  });
}

async function wordInsertParagraph(text: string): Promise<void> {
  await Word.run(async (ctx: Word.RequestContext) => {
    ctx.document.body.insertParagraph(text, "End");
    await ctx.sync();
  });
}

async function wordInsertHtml(html: string): Promise<void> {
  await Word.run(async (ctx: Word.RequestContext) => {
    const sel = ctx.document.getSelection();
    sel.insertHtml(html, "End");
    await ctx.sync();
  });
}

// ---------------------------------------------------------------------------
// Excel
// ---------------------------------------------------------------------------

async function excelGetSelection(): Promise<SelectionInfo> {
  return Excel.run(async (ctx: Excel.RequestContext) => {
    const range = ctx.workbook.getSelectedRange();
    range.load("values,address");
    await ctx.sync();

    const text =
      range.values && range.values.length > 0
        ? range.values.map((row) => row.join("\t")).join("\n")
        : "";

    return { text, location: range.address || "Selection" };
  });
}

async function excelGetSurrounding(): Promise<string> {
  return Excel.run(async (ctx: Excel.RequestContext) => {
    const sheet = ctx.workbook.worksheets.getActiveWorksheet();
    const usedRange = sheet.getUsedRange();
    usedRange.load("values");
    await ctx.sync();

    if (!usedRange.values || usedRange.values.length === 0) return "";
    return usedRange.values
      .slice(0, 20)
      .map((row) => row.join("\t"))
      .join("\n");
  });
}

async function excelGetMeta(): Promise<DocumentMeta> {
  return Excel.run(async (ctx: Excel.RequestContext) => {
    const sheets = ctx.workbook.worksheets;
    sheets.load("items/name");
    const usedRange = ctx.workbook.worksheets
      .getActiveWorksheet()
      .getUsedRange();
    usedRange.load("address");
    await ctx.sync();

    return {
      sheetNames: sheets.items.map((s) => s.name),
      usedRangeSize: usedRange.address || undefined,
    };
  });
}

async function excelWriteToSelection(
  values: (string | number)[][],
): Promise<void> {
  await Excel.run(async (ctx: Excel.RequestContext) => {
    const range = ctx.workbook.getSelectedRange();
    range.load("rowCount,columnCount");
    await ctx.sync();

    const rows = values.length;
    const cols = Math.max(...values.map((r) => r.length));
    const targetRange = range.getResizedRange(
      rows - range.rowCount,
      cols - range.columnCount,
    );
    targetRange.values = values;
    await ctx.sync();
  });
}

async function excelAddComment(
  cellAddress: string,
  text: string,
): Promise<void> {
  await Excel.run(async (ctx: Excel.RequestContext) => {
    const sheet = ctx.workbook.worksheets.getActiveWorksheet();
    sheet.comments.add(cellAddress, text);
    await ctx.sync();
  });
}

// ---------------------------------------------------------------------------
// PowerPoint
// ---------------------------------------------------------------------------

async function pptGetSelection(): Promise<SelectionInfo> {
  const text = await new Promise<string>((resolve) => {
    Office.context.document.getSelectedDataAsync(
      Office.CoercionType.Text,
      (result) => {
        if (result.status === Office.AsyncResultStatus.Succeeded) {
          resolve((result.value as string) || "");
        } else {
          resolve("");
        }
      },
    );
  });

  let location = "Slide";
  try {
    await PowerPoint.run(async (ctx: PowerPoint.RequestContext) => {
      const slides = ctx.presentation.slides;
      slides.load("items");
      await ctx.sync();
      location = `Slide (${slides.items.length} total)`;
    });
  } catch {
    // fall back to generic label
  }

  return { text, location };
}

async function pptGetSurrounding(): Promise<string> {
  return pptGetSelectedSlideText();
}

async function pptGetMeta(): Promise<DocumentMeta> {
  let slideCount = 0;
  try {
    await PowerPoint.run(async (ctx: PowerPoint.RequestContext) => {
      const slides = ctx.presentation.slides;
      slides.load("items");
      await ctx.sync();
      slideCount = slides.items.length;
    });
  } catch {
    // unavailable
  }
  return { slideCount };
}

async function pptGetSelectedSlideText(): Promise<string> {
  return new Promise((resolve) => {
    Office.context.document.getSelectedDataAsync(
      Office.CoercionType.Text,
      (result) => {
        if (result.status === Office.AsyncResultStatus.Succeeded) {
          resolve((result.value as string) || "");
        } else {
          resolve("");
        }
      },
    );
  });
}

async function pptSetSelectedText(text: string): Promise<void> {
  return new Promise((resolve, reject) => {
    Office.context.document.setSelectedDataAsync(
      text,
      { coercionType: Office.CoercionType.Text },
      (result) => {
        if (result.status === Office.AsyncResultStatus.Succeeded) {
          resolve();
        } else {
          reject(new Error(result.error?.message || "Failed to set data"));
        }
      },
    );
  });
}

async function pptAddSlideWithText(text: string): Promise<void> {
  await PowerPoint.run(async (ctx: PowerPoint.RequestContext) => {
    const slides = ctx.presentation.slides;
    slides.add();
    await ctx.sync();

    slides.load("items");
    await ctx.sync();

    const lastSlide = slides.items[slides.items.length - 1];
    const shapes = lastSlide.shapes;
    shapes.load("items");
    await ctx.sync();

    if (shapes.items.length > 0) {
      const shape = shapes.items[0];
      shape.textFrame.textRange.text = text;
      await ctx.sync();
    }
  });
}

// ---------------------------------------------------------------------------
// Outlook
// ---------------------------------------------------------------------------

function outlookGetEmailContext(): Promise<DocumentContext> {
  return new Promise((resolve) => {
    const item = Office.context.mailbox?.item;
    if (!item) {
      resolve({
        hostApp: "Outlook",
        selection: { text: "", location: "No email" },
        surrounding: "",
        documentMeta: {},
      });
      return;
    }

    const parts: string[] = [];
    const meta: DocumentMeta = {};

    const subject =
      typeof item.subject === "string"
        ? item.subject
        : "";
    if (subject) parts.push(`Subject: ${subject}`);

    const from = (item as { from?: { displayName?: string; emailAddress?: string } }).from;
    if (from) parts.push(`From: ${from.displayName || ""} <${from.emailAddress || ""}>`);

    const toRecipients = (item as { to?: { displayName?: string; emailAddress?: string }[] }).to;
    if (toRecipients?.length) {
      parts.push(`To: ${toRecipients.map((r) => r.displayName || r.emailAddress || "").join(", ")}`);
    }

    item.body.getAsync(
      Office.CoercionType.Text,
      (result) => {
        const body =
          result.status === Office.AsyncResultStatus.Succeeded
            ? (result.value as string) || ""
            : "";

        if (body) {
          meta.wordCount = body.split(/\s+/).filter(Boolean).length;
        }

        const surrounding = body.slice(0, 4000);

        resolve({
          hostApp: "Outlook",
          selection: { text: subject, location: "Email" },
          surrounding: parts.join("\n") + (surrounding ? "\n\n" + surrounding : ""),
          documentMeta: meta,
        });
      },
    );
  });
}

function outlookSetBody(text: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const item = Office.context.mailbox?.item;
    if (!item) { reject(new Error("No email item")); return; }

    item.body.setAsync(
      text,
      { coercionType: Office.CoercionType.Text },
      (result) => {
        if (result.status === Office.AsyncResultStatus.Succeeded) resolve();
        else reject(new Error(result.error?.message || "Failed to set body"));
      },
    );
  });
}

function outlookInsertText(text: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const item = Office.context.mailbox?.item;
    if (!item) { reject(new Error("No email item")); return; }

    item.body.setSelectedDataAsync(
      text,
      { coercionType: Office.CoercionType.Text },
      (result) => {
        if (result.status === Office.AsyncResultStatus.Succeeded) resolve();
        else reject(new Error(result.error?.message || "Failed to insert text"));
      },
    );
  });
}

// ---------------------------------------------------------------------------
// Unified context + action dispatch
// ---------------------------------------------------------------------------

export async function getDocumentContext(
  hostApp: OfficeHostApp,
): Promise<DocumentContext> {
  const empty: DocumentContext = {
    hostApp,
    selection: { text: "", location: "Unknown" },
    surrounding: "",
    documentMeta: {},
  };

  try {
    switch (hostApp) {
      case "Word": {
        const [selection, surrounding, meta] = await Promise.all([
          wordGetSelection(),
          wordGetSurrounding(),
          wordGetMeta(),
        ]);
        return { hostApp, selection, surrounding, documentMeta: meta };
      }
      case "Excel": {
        const [selection, surrounding, meta] = await Promise.all([
          excelGetSelection(),
          excelGetSurrounding(),
          excelGetMeta(),
        ]);
        return { hostApp, selection, surrounding, documentMeta: meta };
      }
      case "PowerPoint": {
        const [selection, surrounding, meta] = await Promise.all([
          pptGetSelection(),
          pptGetSurrounding(),
          pptGetMeta(),
        ]);
        return { hostApp, selection, surrounding, documentMeta: meta };
      }
      case "Outlook":
        return outlookGetEmailContext();
      default:
        return empty;
    }
  } catch {
    return empty;
  }
}

/**
 * Put an answer's content into the document the way `action` says. An action
 * this host cannot do throws, so the pane never reports a no-op as applied.
 */
export async function applyAgentAction(
  hostApp: OfficeHostApp,
  action: string,
  content: string,
): Promise<void> {
  switch (action) {
    case "replace_selection":
      if (hostApp === "Word") return wordReplaceSelection(content);
      if (hostApp === "Excel") return excelWriteToSelection(content.split("\n").map((r) => r.split("\t")));
      if (hostApp === "PowerPoint") return pptSetSelectedText(content);
      if (hostApp === "Outlook") return outlookSetBody(content);
      break;

    case "insert_at_end":
      if (hostApp === "Word") return wordInsertAtEnd(content);
      if (hostApp === "PowerPoint") return pptAddSlideWithText(content);
      if (hostApp === "Outlook") return outlookInsertText(content);
      break;

    case "add_comment":
      if (hostApp === "Word") return wordAddComment(content);
      if (hostApp === "Excel") {
        const addr = await Excel.run(async (ctx: Excel.RequestContext) => {
          const r = ctx.workbook.getSelectedRange();
          r.load("address");
          await ctx.sync();
          return r.address;
        });
        return excelAddComment(addr, content);
      }
      break;

    case "insert_paragraph":
      if (hostApp === "Word") return wordInsertParagraph(content);
      break;

    case "insert_table":
    case "insert_html":
      if (hostApp === "Word") return wordInsertHtml(content);
      break;
  }
  throw new Error(`${action.replace(/_/g, " ")} is not available in ${hostApp}`);
}
