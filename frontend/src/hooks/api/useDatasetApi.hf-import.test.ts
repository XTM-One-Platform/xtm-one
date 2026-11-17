// A HuggingFace import whose content matches an existing dataset returns that dataset
// instead of creating one (`reused`), so the page can say nothing was imported.

import { afterEach, describe, expect, it, vi } from "vitest";

import { importHFDataset } from "./useDatasetApi";

/** A server-sent stream carrying the given events, as the import route sends them. */
function streamOf(...events: object[]): Response {
  const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(body));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const PAYLOAD = { hf_dataset_id: "org/set", split: "test", input_column: "q" };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("importHFDataset", () => {
  it("says when the server returned an identical dataset instead of importing one", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamOf({ type: "done", id: "d-1", name: "Existing", reused: true })));

    expect(await importHFDataset(PAYLOAD, () => {})).toEqual({ id: "d-1", name: "Existing", reused: true });
  });

  it("reads a fresh import, after its progress, as not reused", async () => {
    const progress = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamOf({ type: "progress", fetched: 2, total: 4 }, { type: "done", id: "d-2", name: "New" }),
      ),
    );

    expect(await importHFDataset(PAYLOAD, progress)).toEqual({ id: "d-2", name: "New", reused: false });
    expect(progress).toHaveBeenCalledWith({ fetched: 2, total: 4, saving: false });
  });
});
