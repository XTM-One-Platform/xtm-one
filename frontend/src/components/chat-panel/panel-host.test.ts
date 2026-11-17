// The platform's translations as the panel takes them: a key the catalog
// lacks comes back unchanged (the panel then reads its own catalog), and a
// template comes back as written, for the panel to fill.

import { describe, expect, it, vi } from "vitest";

import type { ChatAttachment } from "@filigran/chatbot";
import { agentPageHref, isWorkingFile, PANEL_THEME, panelLocale, panelTranslate } from "@/components/chat-panel/panel-host";

describe("the panel's corners and composer focus (#10910)", () => {
  it("take the design system's radii and focus colour, never a local value", () => {
    expect(PANEL_THEME.radiusSm).toBe("var(--radius-sm)");
    expect(PANEL_THEME.radiusMd).toBe("var(--radius-md)");
    // The composer is a text field: the design-system Textarea's rounded-sm and ring-focus colour.
    expect(PANEL_THEME.inputRadius).toBe("var(--radius-sm)");
    expect(PANEL_THEME.focusRing).toBe("var(--color-filigran-brand-primary)");
    // Its fill is the design-system input fill, not a layer of the page.
    expect(PANEL_THEME.inputSurface).toBe("var(--bg-input-default)");
    // Its border is the Textarea's: none at rest, the input hover tone under the pointer.
    expect(PANEL_THEME.inputBorderHover).toBe("var(--border-input-hover)");
    // The resting height stays the panel's two lines: no host override.
    expect(PANEL_THEME.inputMinHeight).toBeUndefined();
  });
});

describe("the panel's notice tones (#10910)", () => {
  it("take the design system's feedback text tokens, never the panel's amber or red", () => {
    expect(PANEL_THEME.warning).toBe("var(--color-feedback-warning-tertiary)");
    expect(PANEL_THEME.danger).toBe("var(--color-feedback-error-tertiary)");
    expect(PANEL_THEME.success).toBe("var(--color-feedback-success-tertiary)");
  });
});

describe("panelTranslate", () => {
  const format = vi.fn((key: string) => `formatted:${key}`);
  const t = panelTranslate({ Send: "Envoyer", "{agent} has finished": "{agent} a terminé" }, format);

  it("translates a key the platform's catalog holds", () => {
    expect(t("Send")).toBe("formatted:Send");
  });

  it("gives a key the catalog lacks back unchanged", () => {
    expect(t("Conversation transcript")).toBe("Conversation transcript");
  });

  it("gives a template back as written, never formatted without its values", () => {
    format.mockClear();
    expect(t("{agent} has finished")).toBe("{agent} a terminé");
    expect(format).not.toHaveBeenCalled();
  });
});

describe("the panel's notice under the composer", () => {
  it("reads the platform's own words, in every language", async () => {
    for (const locale of ["en", "fr", "es", "ja"]) {
      const messages = (await import(`@/i18n/lang/${locale}.json`)).default as Record<string, string>;
      const t = panelTranslate(messages, (key) => messages[key] ?? key);
      expect(t("Uses AI. Verify results.")).toBe(messages["Uses AI. Verify results."]);
    }
    const en = (await import("@/i18n/lang/en.json")).default as Record<string, string>;
    expect(en["Uses AI. Verify results."]).toBe("AI can make mistakes. Check important information.");
  });
});

describe("panelLocale", () => {
  it("is the platform's language as a BCP 47 tag", () => {
    expect(panelLocale("fr-fr")).toBe("fr-FR");
    expect(panelLocale("not a tag!")).toBe("not a tag!");
  });
});

describe("isWorkingFile", () => {
  const reply = (attachments: ChatAttachment[], content = "Done.") => ({ content, attachments });

  it("collapses a reply's working files, never a deliverable", () => {
    const file = (fileTag?: "download_file" | "working_file") => ({ fileId: "f-1", filename: "out.png", fileTag });
    for (const [tag, folded] of [["working_file", true], ["download_file", false], [undefined, false]] as const) {
      const att = file(tag);
      expect(isWorkingFile(att, reply([att]))).toBe(folded);
    }
  });

  it("collapses a file the reply wrote again under the same name and type, not the newer one", () => {
    const draft = { fileId: "f-1", filename: "brief.md", contentType: "text/markdown" };
    const final = { fileId: "f-2", filename: "brief.md", contentType: "text/markdown; charset=utf-8" };
    const other = { fileId: "f-3", filename: "brief.md", contentType: "text/plain" };
    const message = reply([draft, other, final]);
    expect(isWorkingFile(draft, message)).toBe(true);
    expect(isWorkingFile(final, message)).toBe(false);
    expect(isWorkingFile(other, message)).toBe(false);
  });

  it("finds a file by its id, not by the object the package hands over", () => {
    const draft = { fileId: "f-1", filename: "brief.md", contentType: "text/markdown" };
    const final = { fileId: "f-2", filename: "brief.md", contentType: "text/markdown" };
    const message = reply([draft, final]);
    expect(isWorkingFile({ ...draft }, message)).toBe(true);
    expect(isWorkingFile({ ...final }, message)).toBe(false);
    const alone = { fileId: "f-3", filename: "notes.txt" };
    expect(isWorkingFile({ ...alone }, reply([alone]))).toBe(false);
  });

  it("collapses the files a reply leaves unnamed once it names one of its own", () => {
    const named = { fileId: "AA11-bb22", filename: "report.pdf" };
    const unnamed = { fileId: "cc33-dd44", filename: "notes.txt" };
    expect(isWorkingFile(unnamed, reply([named, unnamed], "Here: [[FILE:aa11-BB22]]"))).toBe(true);
    // A marker naming a file of another reply does not count.
    expect(isWorkingFile(unnamed, reply([named, unnamed], "As before: [[FILE:ee55-ff66]]"))).toBe(false);
  });
});

describe("agentPageHref", () => {
  it("links an agent to its page", () => {
    expect(agentPageHref({ id: "3f2a" })).toBe("/agents/3f2a");
  });

  it("links no picker entry that is not an agent", () => {
    expect(agentPageHref({ id: "general-assistant:" })).toBeUndefined();
    expect(agentPageHref({ id: "flow:9c1" })).toBeUndefined();
  });
});
