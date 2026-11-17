// An answer's markers as the pages outside the chat panel read them: the same
// grammar as the panel, with XTM One's own kinds, the ids each card reads, the
// text a person reads and the one-line preview of a run's result.

import { describe, expect, it } from "vitest";
import type { AnswerPart } from "@filigran/chatbot/markdown";

import {
  XTM_HOST_MARKER_KINDS,
  answerParts,
  answerProse,
  markerId,
  markerRefs,
  summaryPreview,
} from "@/pages/chat/answerMarkers";
import { SECRET_DISPLAY_TEXT } from "@/pages/chat/secretMarkers";
import contract from "@/pages/chat/answer-markers.contract.json";

const A = "11111111-2222-4333-8444-555555555555";
const B = "66666666-7777-4888-9999-000000000000";

const ids = (content: string, kind: string) => markerRefs(answerParts(content), kind).map((ref) => ref.id);

describe("XTM One's marker kinds", () => {
  it("are the chart, the investigation, the work item and the custom dashboard", () => {
    expect(XTM_HOST_MARKER_KINDS).toEqual(["CHART", "DASHBOARD", "INVESTIGATION", "WORK_ITEM"]);
  });
});

describe("markerId", () => {
  it("reads a UUID, lowercase, and nothing else", () => {
    expect(markerId(A.toUpperCase())).toBe(A);
    expect(markerId("not-an-id")).toBeNull();
    expect(markerId(`${A} trailing`)).toBeNull();
  });
});

describe("markerRefs", () => {
  it("reads each kind's ids in order, once each, whatever their case", () => {
    const content = `[[FILE:${B}]] then [[file:${A}]] and [[FILE:${B.toUpperCase()}]]`;
    expect(ids(content, "FILE")).toEqual([B, A]);
    expect(ids(`[[REPORT:${A}]] [[VIDEO:${B}]] [[report:${A}]]`, "REPORT")).toEqual([A]);
    expect(ids(`[[REPORT:${A}]] [[VIDEO:${B}]]`, "VIDEO")).toEqual([B]);
    expect(ids(`[[INVESTIGATION:${A}]] [[WORK_ITEM:${B}]]`, "INVESTIGATION")).toEqual([A]);
    expect(ids(`[[INVESTIGATION:${A}]] [[work_item:${B}]]`, "WORK_ITEM")).toEqual([B]);
  });

  it("keeps a deck's first place and the first slide one of its markers names", () => {
    const content = `[[PRESENTATION:${A}]] [[PRESENTATION:${B}#/s-1]] [[PRESENTATION:${A.toUpperCase()}#/slide_2]] [[PRESENTATION:${A}#/s-3]]`;
    expect(markerRefs(answerParts(content), "PRESENTATION")).toEqual([
      { id: A, slideRef: "slide_2" },
      { id: B, slideRef: "s-1" },
    ]);
  });

  it("skips a marker that names no id", () => {
    expect(ids(`[[REPORT:not-an-id]] [[WORK_ITEM:${A} x]]`, "REPORT")).toEqual([]);
    expect(ids(`[[WORK_ITEM:${A} x]]`, "WORK_ITEM")).toEqual([]);
  });
});

describe("answerProse", () => {
  it("removes every marker, XTM One's own included, and keeps the prose", () => {
    const content = `Here.\n\n[[FILE:${A}]]\n[[PRESENTATION:${A}#/s-1]]\n[[REPORT:${A}]]\n[[VIDEO:${A}]]\n[[INVESTIGATION:${A}]]\n[[WORK_ITEM:${A}]]\nCost: [[CHART:0123456789ab]] done`;
    expect(answerProse(content)).toBe("Here.\n\nCost:  done");
  });

  it("leaves prose with no marker untouched", () => {
    expect(answerProse("  indented\n\n\n\ncode  ")).toBe("  indented\n\n\n\ncode  ");
  });

  it("drops a marker cut off at the end", () => {
    expect(answerProse(`Done. [[REPORT:${A.slice(0, 8)}`)).toBe("Done.");
  });

  it("shows a stored secret as its display text, never the marker", () => {
    const secret = `[[SECRET_VARIABLE:${A}:0123456789abcdef]]`;
    expect(answerProse(`key ${secret} [[FILE:${A}]]`)).toBe(`key ${SECRET_DISPLAY_TEXT}`);
    expect(answerProse(`key ${secret}`)).toBe(`key ${SECRET_DISPLAY_TEXT}`);
  });
});

describe("summaryPreview", () => {
  it("is one marker-free line, cut at the limit", () => {
    const summary = `Built the deck.\n\n[[PRESENTATION:${A}]]\n[[WORK_ITEM:${B}]]\nAll   set.`;
    expect(summaryPreview(summary, 100)).toBe("Built the deck. All set.");
    expect(summaryPreview(summary, 5)).toBe("Built…");
  });
});

/** What one part of an answer names, as the panel's grammar splits it; `null` for prose. */
function namedBy(part: AnswerPart): { kind: string; id: string; slideRef: string | null } | null {
  if (part.type === "file") return { kind: "FILE", id: part.fileId.toLowerCase(), slideRef: null };
  if (part.type === "artifact") return { kind: part.kind.toUpperCase(), id: part.id, slideRef: part.slideRef ?? null };
  if (part.type === "marker") return { kind: part.kind, id: part.args.toLowerCase(), slideRef: null };
  return null;
}

describe("the answer-marker contract with the backend", () => {
  // The same samples the backend's regexes are tested against
  // (answer-markers.contract.json): a shape both sides read the same way.
  it.each(contract.markers)("reads $marker", ({ marker, kind, id, slideRef }) => {
    expect(answerParts(marker).map(namedBy)).toEqual([{ kind, id, slideRef }]);
    // A card reads a UUID; a chart's id is the turn's own.
    if (kind !== "CHART") expect(markerRefs(answerParts(marker), kind)).toEqual([{ id, slideRef }]);
    expect(answerProse(`Before\n\n${marker}\n\nafter`)).toBe("Before\n\nafter");
  });
  it.each(contract.rejected)("reads no marker in %s", (rejected) => {
    expect(markerRefs(answerParts(rejected), "PRESENTATION")).toEqual([]);
  });
});
