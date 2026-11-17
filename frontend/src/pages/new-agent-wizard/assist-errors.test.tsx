// What reaches the wizard assistant's transcript when its round fails
// (AGENT_WIZARD_ASSIST): the route's 503 `{code, message}` is read through the
// real API client, where a 502 would have been "Server unavailable".

import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { ASSIST_ERROR_LABELS, assistErrorLabel } from "./assist-notes";

function answer(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("a failed assist round", () => {
  it.each([
    ["assistant_model_unavailable", "The assistant could not reach the model. Try again in a moment."],
    ["assistant_no_playable_answer", "The assistant could not prepare a change the wizard accepts. Rephrase and try again."],
  ] as const)("keeps the code of a 503 %s for the wizard to word", async (code, message) => {
    answer(503, { detail: { code, message } });
    const error = await api.post("/ai/agent-wizard/assist", { instruction: "hi" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe(message);
    expect(assistErrorLabel(error)).toBe(ASSIST_ERROR_LABELS[code]);
  });

  it("a 502 carries no code: its body never reaches the client", async () => {
    answer(502, { detail: { code: "assistant_model_unavailable", message: "x" } });
    const error = await api.post("/ai/agent-wizard/assist", { instruction: "hi" }).catch((e: unknown) => e);
    expect((error as Error).message).toBe("Server unavailable (HTTP 502)");
    expect(assistErrorLabel(error)).toBeNull();
  });
});
