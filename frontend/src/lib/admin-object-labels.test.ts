// The Status column of Admin > Objects never shows a raw code: a stored state
// or kind reads its catalog label, a file's content type its format, and any
// other value is humanised (UX round 2 of #11938).

import { describe, expect, it } from "vitest";

import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { objectValueText } from "./admin-object-labels";

const inFrench = (id: string) => (fr as Record<string, string>)[id] ?? id;
const inEnglish = (id: string) => (en as Record<string, string>)[id] ?? id;

describe("objectValueText", () => {
  it("reads a run, a work item and a conversation in words", () => {
    expect(objectValueText("awaiting_human", inEnglish)).toBe("Awaiting input");
    expect(objectValueText("user", inEnglish)).toBe("User");
    expect(objectValueText("manual", inEnglish)).toBe("Manual");
    expect(objectValueText("agent", inEnglish)).toBe("Agent");
    expect(objectValueText("web", inEnglish)).toBe("Web Chat");
    expect(objectValueText("whatsapp", inEnglish)).toBe("WhatsApp");
  });

  it("reads a work item's open state as the adjective, not the verb", () => {
    expect(objectValueText("open", inEnglish)).toBe("Open");
    expect(objectValueText("open", inFrench)).toBe("Ouvert");
  });

  it("translates the labels", () => {
    expect(objectValueText("awaiting_human", inFrench)).toBe("En attente de réponse");
    expect(objectValueText("dataset", inFrench)).toBe("Jeu de données");
  });

  it("reads a file's content type as its format", () => {
    expect(objectValueText("image/png", inEnglish)).toBe("PNG");
    expect(objectValueText("image/svg+xml", inEnglish)).toBe("SVG");
    expect(objectValueText("application/pdf", inEnglish)).toBe("PDF");
  });

  it("humanises a value with no label", () => {
    expect(objectValueText("some_new_kind", inEnglish)).toBe("Some new kind");
  });

  // Every origin the backend writes on FileAttachment.source, and every
  // Workset.status (UX round 3 of #11938): each reads a key all four catalogs hold.
  const FILE_SOURCES = [
    "chat", "public_chat", "assignment", "knowledge_base", "agent", "dataset", "template_asset",
    "agent_generated", "mcp", "tool_test", "flow", "dashboard_asset", "presentation_import",
    "presentation_video_import", "widget_upload", "signature", "runner_capture", "runner_capture_sidecar",
    "browser_profile_mfa", "sandbox_workspace", "agent_working", "agent_download", "video_take",
    "video_clip", "video_render", "video_mfa", "video_voice", "video_music", "video_upload",
    "clip-start-frame", "channel:slack", "account:microsoft",
  ];
  const WORKSET_STATUSES = ["active", "resolved"];
  const catalogs = { en, fr, es, ja } as Record<string, Record<string, string>>;

  it.each([...FILE_SOURCES, ...WORKSET_STATUSES])("reads %s through a key of every catalog", (value) => {
    const keys: string[] = [];
    objectValueText(value, (id) => {
      keys.push(id);
      return id;
    });
    expect(keys).toHaveLength(1);
    for (const [lang, catalog] of Object.entries(catalogs)) {
      expect(catalog[keys[0]], `${lang}: ${keys[0]}`).toBeTruthy();
    }
  });

  it("translates a resolved workset and a rendered video", () => {
    expect(objectValueText("resolved", inFrench)).toBe("Résolu");
    expect(objectValueText("video_render", inFrench)).toBe("Vidéo rendue");
  });
});
