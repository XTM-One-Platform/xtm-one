// The "Download failed" toast says what happened in the reader's language,
// never the backend's raw reason.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import en from "@/i18n/lang/en.json";
import fr from "@/i18n/lang/fr.json";
import { downloadErrorDescription, downloadErrorStatus } from "@/lib/download-error";

type Catalog = Record<string, string>;

function translator(catalog: Catalog) {
  return (id: string, values?: Record<string, string | number>) => {
    let text = catalog[id];
    if (text === undefined) throw new Error(`missing i18n key: ${id}`);
    for (const [name, value] of Object.entries(values ?? {})) text = text.split(`{${name}}`).join(String(value));
    return text;
  };
}

const RAW = "File not found";

function httpError(status: number) {
  return Object.assign(new Error(`HTTP ${status}: ${RAW}`), { status });
}

let warn: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  warn.mockRestore();
});

describe("downloadErrorDescription", () => {
  it("says a 404 file no longer exists, translated", () => {
    expect(downloadErrorDescription(httpError(404), "report.pdf", translator(fr as Catalog))).toBe(
      "«\u00a0report.pdf\u00a0» n'existe plus.",
    );
    expect(downloadErrorDescription(httpError(404), "", translator(en as Catalog))).toBe(
      "This file no longer exists.",
    );
  });

  it("says the reader has no access on a 403, translated", () => {
    expect(downloadErrorDescription(httpError(403), "report.pdf", translator(fr as Catalog))).toBe(
      "Vous n'avez pas accès à «\u00a0report.pdf\u00a0».",
    );
    expect(downloadErrorDescription(httpError(403), null, translator(en as Catalog))).toBe(
      "You do not have access to this file.",
    );
  });

  it("gives the generic translated failure for any other error", () => {
    expect(downloadErrorDescription(httpError(500), "report.pdf", translator(en as Catalog))).toBe(
      "Could not download \"report.pdf\". Please try again.",
    );
    expect(downloadErrorDescription(new TypeError("Failed to fetch"), undefined, translator(fr as Catalog))).toBe(
      "Impossible de télécharger ce fichier. Veuillez réessayer.",
    );
  });

  it("never puts the raw backend reason in the toast, and logs it instead", () => {
    for (const status of [403, 404, 500]) {
      for (const name of ["report.pdf", ""]) {
        expect(downloadErrorDescription(httpError(status), name, translator(fr as Catalog))).not.toContain(RAW);
      }
    }
    expect(warn).toHaveBeenCalled();
  });

  it("is the one mapping of every download toast, in the chat and outside it", () => {
    // vitest runs from frontend/ (CI and `yarn test`), as chat-unavailable-copy.test.ts.
    const files = [
      "components/chat/ChatMarkdown.tsx",
      "components/chat-panel/FileChips.tsx",
      "pages/chat/ConversationFilesPanel.tsx",
      "pages/KnowledgeBaseDetail.tsx",
      "components/ui/xtm-mcp-package-download.tsx",
    ];
    for (const path of files) {
      const source = readFileSync(join(process.cwd(), "src", path), "utf8");
      const toasts = source.split('t_i18n("Download failed")').slice(1);
      expect(toasts.length, path).toBeGreaterThan(0);
      for (const after of toasts) {
        // The toast's description is the next property: the shared helper, never err.message.
        const description = after.slice(0, after.indexOf("});"));
        expect(description, path).toContain("downloadErrorDescription(err");
        expect(description, path).not.toContain(".message");
      }
    }
  });

  it("keeps the fr guillemets on the file name's line (non-breaking spaces)", () => {
    const t = translator(fr as Catalog);
    for (const status of [403, 404, 500]) {
      const text = downloadErrorDescription(httpError(status), "restricted-board-memo.docx", t);
      expect(text).toContain("«\u00a0restricted-board-memo.docx\u00a0»");
      expect(text).not.toMatch(/« | »/);
    }
  });

  it("reads the status an error carries, nothing else", () => {
    expect(downloadErrorStatus(httpError(404))).toBe(404);
    expect(downloadErrorStatus(new Error("HTTP 404"))).toBeUndefined();
    expect(downloadErrorStatus(null)).toBeUndefined();
    expect(downloadErrorStatus("404")).toBeUndefined();
  });
});
