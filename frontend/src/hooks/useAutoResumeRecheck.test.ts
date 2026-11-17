// The interrupted notice of a turn queued for its automatic resume reads the
// conversation again once, so Resume comes back without a reload when
// the turn left the queue without resuming.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { AUTO_RESUME_RECHECK_MS, useAutoResumeRecheck } from "@/hooks/useAutoResumeRecheck";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useAutoResumeRecheck", () => {
  it("waits the same-build deferral plus one sweep", () => {
    expect(AUTO_RESUME_RECHECK_MS).toBe(100_000);
  });

  it("refetches once after the wait while the turn resumes by itself", () => {
    const refetch = vi.fn();
    renderHook(() => useAutoResumeRecheck("c-1", true, refetch));

    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS - 1);
    expect(refetch).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(refetch).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS * 3);
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("does not restart the wait when only the refetch function changes", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ fn }) => useAutoResumeRecheck("c-1", true, fn), {
      initialProps: { fn: first },
    });

    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS / 2);
    rerender({ fn: second });
    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS / 2);

    expect(first.mock.calls.length + second.mock.calls.length).toBe(1);
  });

  it("does nothing when the notice offers Resume", () => {
    const refetch = vi.fn();
    renderHook(() => useAutoResumeRecheck("c-1", false, refetch));

    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS * 2);
    expect(refetch).not.toHaveBeenCalled();
  });

  it("cancels the wait when the turn leaves the queue or the conversation changes", () => {
    const refetch = vi.fn();
    const { rerender, unmount } = renderHook(
      ({ cid, auto }) => useAutoResumeRecheck(cid, auto, refetch),
      { initialProps: { cid: "c-1", auto: true } },
    );

    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS / 2);
    rerender({ cid: "c-1", auto: false });
    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS);
    expect(refetch).not.toHaveBeenCalled();

    rerender({ cid: "c-2", auto: true });
    unmount();
    vi.advanceTimersByTime(AUTO_RESUME_RECHECK_MS * 2);
    expect(refetch).not.toHaveBeenCalled();
  });
});
