// The agent filter's owners read waits on the same gate as the list and the
// counts: while the Work Items tab settles a `?fleet=` scope, no
// `/work-items/owners?...&fleet=` request goes out.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { useWorkItemOwners } from "./useWorkItemApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

function Owners({ enabled }: { enabled?: boolean }) {
  const { data } = useWorkItemOwners(
    { vis: "mine", fleet: "f-1" },
    enabled === undefined ? undefined : { enabled },
  );
  return <p>{data ? `owners ${data.length}` : "waiting"}</p>;
}

function renderOwners(enabled?: boolean) {
  return renderWithProviders(
    <Owners enabled={enabled} />,
    new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  );
}

afterEach(() => {
  get.mockReset();
});

describe("useWorkItemOwners", () => {
  it("sends nothing while it is held", async () => {
    get.mockResolvedValue([]);
    renderOwners(false);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(get).not.toHaveBeenCalled();
    expect(screen.getByText("waiting")).toBeTruthy();
  });

  it("reads the owners with the scope once enabled", async () => {
    get.mockResolvedValue([]);
    renderOwners(true);
    expect(await screen.findByText("owners 0")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/work-items/owners?vis=mine&fleet=f-1");
  });

  it("reads at once when no option is given", async () => {
    get.mockResolvedValue([]);
    renderOwners();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
  });
});
