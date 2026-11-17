// A dataset ZIP goes to the server up to the backend's 500 MB package ceiling and a
// file attached to an entry up to its 40 MB upload ceiling: the page refuses only
// what the server would refuse, never a package of a few tens of MB.

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { testQueryClient, withProviders } from "@/testing/providers";
import { useImportDatasetZip, useUploadDatasetEntryFile } from "./useDatasetApi";

const MB = 1024 * 1024;

/** A file that reports *size* bytes without holding them. */
function fileOf(name: string, size: number, type = ""): File {
  const file = new File(["x"], name, { type });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

function stubFetch() {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "x" }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("dataset ZIP import size", () => {
  const importZip = () => renderHook(() => useImportDatasetZip(), { wrapper: withProviders(testQueryClient()) }).result;

  it("sends a 25 MB package to the server", async () => {
    const fetchMock = stubFetch();
    const result = importZip();

    await act(() => result.current.mutateAsync(fileOf("set.zip", 25 * MB, "application/zip")));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/datasets\/import$/);
  });

  it("sends a package of exactly 500 MB", async () => {
    const fetchMock = stubFetch();
    const result = importZip();

    await act(() => result.current.mutateAsync(fileOf("set.zip", 500 * MB, "application/zip")));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("refuses a package over 500 MB without sending it", async () => {
    const fetchMock = stubFetch();
    const result = importZip();

    await expect(
      act(() => result.current.mutateAsync(fileOf("set.zip", 500 * MB + 1, "application/zip"))),
    ).rejects.toThrow("File is too large (max 500 MB)");
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });
});

describe("dataset entry file size", () => {
  const upload = () =>
    renderHook(() => useUploadDatasetEntryFile(), { wrapper: withProviders(testQueryClient()) }).result;

  it("sends a file of exactly 40 MB", async () => {
    const fetchMock = stubFetch();
    const result = upload();

    await act(() => result.current.mutateAsync({ entryId: "e-1", file: fileOf("doc.pdf", 40 * MB) }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/datasets\/entries\/e-1\/upload$/);
  });

  it("refuses a file over 40 MB without sending it", async () => {
    const fetchMock = stubFetch();
    const result = upload();

    await expect(
      act(() => result.current.mutateAsync({ entryId: "e-1", file: fileOf("doc.pdf", 40 * MB + 1) })),
    ).rejects.toThrow("File is too large (max 40 MB)");
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });
});
