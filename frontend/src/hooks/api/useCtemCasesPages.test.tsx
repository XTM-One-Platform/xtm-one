// The Cases list reads every page of GET /ctem-cases (#2377): the API caps a page at 200, so a
// deployment with more cases showed only the first 200. The other pages are read in parallel, and
// the bulk routes (200 ids a call) get a larger selection in runs.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import {
  CTEM_CASES_PAGE,
  chunkIds,
  readAllCtemCases,
  useBulkUpdateCtemCases,
  useCtemCases,
  useCtemCasesForAsset,
  useDeleteCtemCases,
} from "@/hooks/api/useCtemCasesApi";
import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const TOTAL = 514;
const ALL = Array.from({ length: TOTAL }, (_, i) => ({ id: `c-${String(i).padStart(3, "0")}`, state: "open" }));

/** A mocked API holding TOTAL cases, one page per (limit, offset). */
function serve(path: string) {
  const url = new URL(path, "http://x");
  const limit = Number(url.searchParams.get("limit") ?? 50);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  return Promise.resolve({
    vertical: "default",
    counts: { open: TOTAL, triaged: 0, remediating: 0 },
    cases: ALL.slice(offset, offset + limit),
    details_unavailable: [],
  });
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
});

describe("readAllCtemCases", () => {
  it("reads every page and keeps each case once", async () => {
    get.mockImplementation(serve as never);
    const list = await readAllCtemCases();
    expect(list.cases).toHaveLength(TOTAL);
    expect(new Set(list.cases.map((c) => c.id)).size).toBe(TOTAL);
    expect(get.mock.calls.map(([p]) => p)).toEqual([
      "/ctem-cases?limit=200&offset=0",
      "/ctem-cases?limit=200&offset=200",
      "/ctem-cases?limit=200&offset=400",
    ]);
  });

  it("reads one page when the cases fit in it", async () => {
    get.mockResolvedValue({ vertical: "default", counts: { open: 3 }, cases: ALL.slice(0, 3), details_unavailable: [] } as never);
    const list = await readAllCtemCases("asset=web-01");
    expect(list.cases).toHaveLength(3);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith("/ctem-cases?limit=200&offset=0&asset=web-01");
  });

  it("keeps a case seen on two pages once", async () => {
    get.mockImplementation(((path: string) =>
      serve(path).then((page) => (path.includes("offset=200") ? { ...page, cases: [ALL[199], ...page.cases] } : page))) as never);
    const list = await readAllCtemCases();
    expect(list.cases).toHaveLength(TOTAL);
  });
});

describe("the case list hooks", () => {
  it("useCtemCases holds every case of a 3-page list", async () => {
    get.mockImplementation(serve as never);
    const { result } = renderHook(() => useCtemCases(), { wrapper: withProviders(testQueryClient()) });
    await waitFor(() => expect(result.current.data?.cases).toHaveLength(TOTAL));
  });

  it("useCtemCasesForAsset reads every page of the asset's cases", async () => {
    get.mockImplementation(serve as never);
    const { result } = renderHook(() => useCtemCasesForAsset("web-01"), { wrapper: withProviders(testQueryClient()) });
    await waitFor(() => expect(result.current.data?.cases).toHaveLength(TOTAL));
    expect(get).toHaveBeenCalledWith("/ctem-cases?limit=200&offset=400&asset=web-01");
  });
});

describe("bulk changes past 200 cases", () => {
  it("chunkIds cuts in runs of 200", () => {
    expect(chunkIds(ALL.map((c) => c.id)).map((r) => r.length)).toEqual([CTEM_CASES_PAGE, CTEM_CASES_PAGE, 114]);
    expect(chunkIds([])).toEqual([]);
  });

  it("a bulk change is sent in runs and the answers merged", async () => {
    post.mockImplementation(((_path: string, body: { case_ids: string[] }) =>
      Promise.resolve({ updated: body.case_ids, failed: [] })) as never);
    const { result } = renderHook(() => useBulkUpdateCtemCases(), { wrapper: withProviders(testQueryClient()) });
    let answer: Awaited<ReturnType<typeof result.current.mutateAsync>> | undefined;
    await act(async () => {
      answer = await result.current.mutateAsync({ case_ids: ALL.map((c) => c.id), state: "remediating" });
    });
    expect(post).toHaveBeenCalledTimes(3);
    expect(answer?.updated).toHaveLength(TOTAL);
  });

  it("a delete past 200 cases is sent in runs and the counts added", async () => {
    post.mockImplementation(((_path: string, body: { case_ids: string[]; dry_run: boolean }) =>
      Promise.resolve({ dry_run: body.dry_run, count: body.case_ids.length, cases: [], unknown: [] })) as never);
    const { result } = renderHook(() => useDeleteCtemCases(), { wrapper: withProviders(testQueryClient()) });
    let answer: Awaited<ReturnType<typeof result.current.mutateAsync>> | undefined;
    await act(async () => {
      answer = await result.current.mutateAsync({ case_ids: ALL.map((c) => c.id), dry_run: true });
    });
    expect(post).toHaveBeenCalledTimes(3);
    expect(post.mock.calls.every(([, body]) => (body as { case_ids: string[] }).case_ids.length <= CTEM_CASES_PAGE)).toBe(true);
    expect(answer).toMatchObject({ dry_run: true, count: TOTAL });
  });
});
