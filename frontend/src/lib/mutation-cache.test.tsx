// The app's MutationCache (`main.tsx`): what every write does once it settles.

import { describe, expect, it } from "vitest";
import { act, renderHook, screen } from "@testing-library/react";
import { useMutation } from "@tanstack/react-query";

import { testQueryClient, withProviders } from "@/testing/providers";

function settle(meta: Record<string, unknown> | undefined, outcome: "succeeds" | "fails") {
  const client = testQueryClient({ appMutationCache: true });
  client.setQueryData(["exposure", "agent-1"], { shared: 0 });
  const { result } = renderHook(
    () =>
      useMutation({
        meta,
        mutationFn: () => (outcome === "succeeds" ? Promise.resolve("ok") : Promise.reject(new Error("Refused by the server"))),
      }),
    { wrapper: withProviders(client) },
  );
  return { client, run: () => act(() => result.current.mutateAsync().catch(() => undefined)) };
}

const exposureInvalidated = (client: ReturnType<typeof testQueryClient>) =>
  client.getQueryState(["exposure", "agent-1"])?.isInvalidated;

describe("createAppMutationCache", () => {
  it("reads the exposure again after every write", async () => {
    const { client, run } = settle(undefined, "succeeds");
    await run();
    expect(exposureInvalidated(client)).toBe(true);
  });

  it("leaves the exposure alone after a read-only call", async () => {
    const { client, run } = settle({ readOnly: true }, "succeeds");
    await run();
    expect(exposureInvalidated(client)).toBe(false);
  });

  it("shows a failed write's reason in the global error toast", async () => {
    const { run } = settle(undefined, "fails");
    await run();
    expect(await screen.findByText("Refused by the server")).toBeInTheDocument();
  });

  it("stays quiet for a mutation whose callers toast the failure themselves", async () => {
    const { run } = settle({ suppressGlobalErrorToast: true }, "fails");
    await run();
    expect(screen.queryByText("Refused by the server")).toBeNull();
  });
});
