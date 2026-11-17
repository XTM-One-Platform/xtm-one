// The conversation on screen survives a backend restart: read again at a
// turn's end while the backend is down, it stays on screen and is read again
// until the backend is back; only a first read that fails reaches the boundary.

import { Component, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { AUTO_RESUME_RECHECK_MS } from "@/hooks/useAutoResumeRecheck";
import { api, BackendUnavailableError } from "@/lib/api";
import { withProviders } from "@/testing/providers";
import type { ConversationDetail } from "@/types";

import {
  CONVERSATION_BACKEND_RETRIES,
  CONVERSATION_TURN_POLL_MS,
  retryConversationRead,
  useConversation,
} from "./useChatApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);
const conversation = (title: string) => ({ id: "c1", title, messages: [] }) as unknown as ConversationDetail;
const down = () => new BackendUnavailableError("Server unavailable (HTTP 502)");

beforeEach(() => {
  get.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("reading the conversation again", () => {
  it("goes on while the backend is unreachable, for a while", () => {
    expect(retryConversationRead(0, down(), false)).toBe(true);
    expect(retryConversationRead(CONVERSATION_BACKEND_RETRIES - 1, down(), false)).toBe(true);
    expect(retryConversationRead(CONVERSATION_BACKEND_RETRIES, down(), false)).toBe(false);
  });

  it("asks any other failure once, and a 404 the caller expects never", () => {
    const failed = Object.assign(new Error("boom"), { status: 500 });
    expect(retryConversationRead(0, failed, true)).toBe(true);
    expect(retryConversationRead(1, failed, true)).toBe(false);
    expect(retryConversationRead(0, Object.assign(new Error("Not found"), { status: 404 }), false)).toBe(false);
  });
});

describe("useConversation", () => {
  it("keeps the conversation on screen through a backend restart, and takes the read that follows", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0, gcTime: Infinity } } });
    get.mockResolvedValueOnce(conversation("Before"));
    const { result } = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(client),
    });
    await waitFor(() => expect(result.current.data?.title).toBe("Before"));

    for (let i = 0; i <= CONVERSATION_BACKEND_RETRIES; i += 1) get.mockRejectedValueOnce(down());
    await client.invalidateQueries({ queryKey: ["conversation", "c1"] }).catch(() => undefined);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data?.title).toBe("Before");
    expect(get).toHaveBeenCalledTimes(CONVERSATION_BACKEND_RETRIES + 2);

    get.mockResolvedValueOnce(conversation("After"));
    await client.invalidateQueries({ queryKey: ["conversation", "c1"] });
    await waitFor(() => expect(result.current.data?.title).toBe("After"));
  });

  it("reads a conversation whose turn resumes by itself once more after the resume window, then stops", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const queued = { ...conversation("Queued"), interrupted_turn: { auto_resume: true } } as unknown as ConversationDetail;
    const dropped = { ...conversation("Dropped"), interrupted_turn: { auto_resume: false } } as unknown as ConversationDetail;
    get.mockResolvedValueOnce(queued).mockResolvedValueOnce(dropped);
    const { result } = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(new QueryClient()),
    });
    await waitFor(() => expect(result.current.data?.title).toBe("Queued"));
    expect(get).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(AUTO_RESUME_RECHECK_MS);
    await waitFor(() => expect(result.current.data?.title).toBe("Dropped"));
    expect(get).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(AUTO_RESUME_RECHECK_MS * 3);
    expect(get, "no polling once the notice offers Resume").toHaveBeenCalledTimes(2);
  });

  it("never reads again a conversation whose interrupted turn offers Resume", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    get.mockResolvedValue({ ...conversation("Offered"), interrupted_turn: { auto_resume: false } } as unknown as ConversationDetail);
    const { result } = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(new QueryClient()),
    });
    await waitFor(() => expect(result.current.data?.title).toBe("Offered"));

    await vi.advanceTimersByTimeAsync(AUTO_RESUME_RECHECK_MS * 2);
    expect(get).toHaveBeenCalledTimes(1);
  });

  // #10910 B1: a page that left the conversation during a turn and comes back
  // reads it again, never trusting the copy cached mid-turn.
  it("reads again on return a conversation cached while a turn was in flight", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, gcTime: Infinity } } });
    const inFlight = { ...conversation("Working"), is_processing: true } as unknown as ConversationDetail;
    const ended = { ...conversation("Answered"), is_processing: false } as unknown as ConversationDetail;
    get.mockResolvedValueOnce(inFlight).mockResolvedValueOnce(ended);
    const first = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(client),
    });
    await waitFor(() => expect(first.result.current.data?.title).toBe("Working"));
    first.unmount();

    const back = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(client),
    });
    await waitFor(() => expect(back.result.current.data?.title).toBe("Answered"));
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("keeps a conversation with no turn in flight fresh on return", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
    get.mockResolvedValue({ ...conversation("Idle"), is_processing: false } as unknown as ConversationDetail);
    const first = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(client),
    });
    await waitFor(() => expect(first.result.current.data?.title).toBe("Idle"));
    first.unmount();
    const back = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(client),
    });
    expect(back.result.current.data?.title).toBe("Idle");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("follows a turn it has no stream of until the conversation says it ended, then stops", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const inFlight = { ...conversation("Working"), is_processing: true } as unknown as ConversationDetail;
    const ended = { ...conversation("Answered"), is_processing: false } as unknown as ConversationDetail;
    get.mockResolvedValueOnce(inFlight).mockResolvedValueOnce(inFlight).mockResolvedValue(ended);
    const { result } = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true, followTurn: true }), {
      wrapper: withProviders(new QueryClient()),
    });
    await waitFor(() => expect(result.current.data?.title).toBe("Working"));

    await vi.advanceTimersByTimeAsync(CONVERSATION_TURN_POLL_MS * 2);
    await waitFor(() => expect(result.current.data?.title).toBe("Answered"));
    const reads = get.mock.calls.length;

    await vi.advanceTimersByTimeAsync(CONVERSATION_TURN_POLL_MS * 3);
    expect(get, "no polling once the turn ended").toHaveBeenCalledTimes(reads);
  });

  it("never polls a turn in flight for a host that follows its stream", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    get.mockResolvedValue({ ...conversation("Working"), is_processing: true } as unknown as ConversationDetail);
    const { result } = renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
      wrapper: withProviders(new QueryClient()),
    });
    await waitFor(() => expect(result.current.data?.title).toBe("Working"));
    await vi.advanceTimersByTimeAsync(CONVERSATION_TURN_POLL_MS * 3);
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("throws a first read that fails to the page's boundary", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } });
    get.mockRejectedValue(Object.assign(new Error("boom"), { status: 500 }));
    const caught: unknown[] = [];
    class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
      state = { failed: false };
      static getDerivedStateFromError() {
        return { failed: true };
      }
      componentDidCatch(error: unknown) {
        caught.push(error);
      }
      render() {
        return this.state.failed ? null : this.props.children;
      }
    }
    const Providers = withProviders(client);
    const onError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      renderHook(() => useConversation("c1", { notFoundIsOrdinary: true }), {
        wrapper: ({ children }) => (
          <Providers>
            <Boundary>{children}</Boundary>
          </Providers>
        ),
      });
      await waitFor(() => expect(caught).toHaveLength(1));
    } finally {
      onError.mockRestore();
    }
  });
});
