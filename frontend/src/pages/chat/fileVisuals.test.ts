// The type label of a file chip stays short.

import { describe, expect, it } from "vitest";

import { mimeLabel, resolveAttachmentMime } from "./fileVisuals";

const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

describe("mimeLabel", () => {
  it("keeps the type token the attachment carries", () => {
    expect(mimeLabel({ type: "pdf" }, "application/pdf")).toBe("PDF");
    expect(mimeLabel({ type: " csv " }, "text/csv")).toBe("CSV");
  });

  it("maps a long known MIME type to its short type, never the raw subtype", () => {
    expect(mimeLabel({ content_type: DOCX }, DOCX)).toBe("DOCX");
    expect(mimeLabel({ content_type: XLSX }, resolveAttachmentMime({ content_type: XLSX }))).toBe("XLSX");
    expect(mimeLabel({}, "image/jpeg")).toBe("JPG");
    expect(mimeLabel({}, "text/javascript")).toBe("JS");
  });

  it("falls back to the subtype for an unknown MIME type, and FILE without one", () => {
    expect(mimeLabel({}, "application/zip")).toBe("ZIP");
    expect(mimeLabel({}, "application/")).toBe("FILE");
    expect(mimeLabel({}, "")).toBe("FILE");
  });
});
