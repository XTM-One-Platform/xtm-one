import { describe, expect, it } from "vitest";
import { isLongNote, linkifyIds, splitDescription } from "./work-item-notes";

const RUN = "8f240eba-1111-4222-8333-944445555666";
const RUN2 = "19f6aaaa-1111-4222-8333-944445555666";

describe("splitDescription", () => {
  it("leaves a description without stamps as the brief", () => {
    expect(splitDescription("Do the thing.\n\nThen the other.")).toEqual({
      notes: [],
      brief: "Do the thing.\n\nThen the other.",
    });
    expect(splitDescription(null)).toEqual({ notes: [], brief: "" });
  });

  it("parses a stamp with its run and one without", () => {
    const { notes, brief } = splitDescription(
      `2026-11-20 23:22Z (run ${RUN}): **Slice 5 pushed**\n\n2026-11-20 22:18Z: ANSWERED by Sam: go\n\nThe brief.`,
    );
    expect(notes).toEqual([
      { at: "2026-11-20T23:22:00Z", runId: RUN, body: "**Slice 5 pushed**" },
      { at: "2026-11-20T22:18:00Z", runId: null, body: "ANSWERED by Sam: go" },
    ]);
    expect(brief).toBe("The brief.");
  });

  it("keeps the paragraphs of a note together up to the next stamp", () => {
    const { notes, brief } = splitDescription(
      `2026-11-20 23:22Z (run ${RUN}): lead\n\n- bullet\n\n2026-11-20 22:00Z (run ${RUN2}): started\n\nBrief here.`,
    );
    expect(notes[0].body).toBe("lead\n\n- bullet");
    expect(notes[1].body).toBe("started");
    expect(brief).toBe("Brief here.");
  });

  it("drops a stamp repeated inside the body", () => {
    const nested = splitDescription(`2026-11-20 22:21Z (run ${RUN2}): 2026-11-20 22:23Z (run ${RUN2}, fast path): reopened`);
    expect(nested.notes[0].body).toBe("reopened");
    const trailing = splitDescription(`2026-11-20 22:21Z (run ${RUN}): DM'd to Sam (run 8f240eba...)`);
    expect(trailing.notes[0].body).toBe("DM'd to Sam");
    const time = splitDescription(`2026-11-20 22:21Z (run ${RUN}): 22:21Z: done`);
    expect(time.notes[0].body).toBe("done");
  });

  it("reads a stamp written with a T and seconds", () => {
    const { notes } = splitDescription(`2026-11-20T22:21:09Z (run ${RUN}): x`);
    expect(notes[0]).toMatchObject({ at: "2026-11-20T22:21:00Z", runId: RUN, body: "x" });
  });
});

describe("linkifyIds", () => {
  it("turns uuids, 40-hex shas and long digit ids into chips", () => {
    const out = linkifyIds(`run ${RUN} head b6ce1323be6d239a33100a2b6f20a1918298aad3 msg 1795175038574`);
    expect(out).toContain(`[8f240eba](#inline-marker/ID/${RUN})`);
    expect(out).toContain("[b6ce1323](#inline-marker/ID/b6ce1323be6d239a33100a2b6f20a1918298aad3)");
    expect(out).toContain("[17951750\u2026](#inline-marker/ID/1795175038574)");
  });

  it("links owner/repo#N, and a bare #N and a sha when the repository is known", () => {
    const out = linkifyIds("see XTM-One-Platform/xtm-one#780 and #776 at b6ce1323be6d239a33100a2b6f20a1918298aad3", "xtm-one-platform/xtm-one");
    expect(out).toContain("[XTM-One-Platform/xtm-one#780](https://github.com/XTM-One-Platform/xtm-one/issues/780)");
    // No opening bracket before the bare issue ref: the colour-literal gate reads a bracket, a hash and 3+ hex digits as a colour.
    expect(out).toContain("#776](https://github.com/xtm-one-platform/xtm-one/issues/776)");
    expect(out).toContain("[b6ce1323](https://github.com/xtm-one-platform/xtm-one/commit/b6ce1323be6d239a33100a2b6f20a1918298aad3)");
  });

  it("leaves code spans, fences, links, dates and short numbers as written", () => {
    const src = `\`${RUN}\` [a](https://x.test/${RUN}) 2026-11-20 run 44836335 #776\n\`\`\`\n${RUN}\n\`\`\``;
    expect(linkifyIds(src)).toBe(src);
  });
});

describe("isLongNote", () => {
  it("folds a note past six lines", () => {
    expect(isLongNote("one line")).toBe(false);
    expect(isLongNote(Array.from({ length: 7 }, (_, i) => `line ${i}`).join("\n"))).toBe(true);
  });
});
