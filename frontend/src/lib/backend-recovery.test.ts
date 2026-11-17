import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BACKEND_RECOVERY_POLL_MS, useBackendRecovery } from "@/lib/backend-recovery";

describe("useBackendRecovery (#10910)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("asks the liveness route until it answers, then calls back once", async () => {
    const answers = [new TypeError("Failed to fetch"), { ok: false }, { ok: true }, { ok: true }];
    const fetchMock = vi.fn(async (_url: string) => {
      const next = answers.shift();
      if (next instanceof Error) throw next;
      return next as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    const onBack = vi.fn();
    renderHook(() => useBackendRecovery(true, onBack));

    await vi.advanceTimersByTimeAsync(BACKEND_RECOVERY_POLL_MS);
    await vi.advanceTimersByTimeAsync(BACKEND_RECOVERY_POLL_MS);
    expect(onBack).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(BACKEND_RECOVERY_POLL_MS);
    expect(onBack).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(BACKEND_RECOVERY_POLL_MS * 3);
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0]?.[0]).toMatch(/\/api\/health$/);
  });

  it("asks nothing when disabled", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderHook(() => useBackendRecovery(false, vi.fn()));
    await vi.advanceTimersByTimeAsync(BACKEND_RECOVERY_POLL_MS * 3);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
