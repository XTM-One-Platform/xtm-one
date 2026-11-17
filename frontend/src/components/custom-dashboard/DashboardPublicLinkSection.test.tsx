// A published link's marking ceiling, as its managers set it: picked by name among the OpenCTI markings the
// dashboard's integration has, sent whole; no picker for a dashboard that reads no OpenCTI.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { DashboardPublicLinkSection } from "./DashboardPublicLinkSection";

const ID = "11111111-1111-4111-8111-111111111111";
const LINK = {
  enabled_on_platform: true, url: "https://xtm.example/public/dashboards/t", path: "/public/dashboards/t", created_at: null,
  publisher_name: "Ada", refresh_minutes: 60, snapshot_at: null, marking_ceiling: [],
}; // prettier-ignore

function serve(sources: Record<string, unknown>) {
  api.get.mockImplementation(async (path: string) =>
    path.endsWith("/public-link") ? LINK : { id: ID, revision: 1, spec: { version: 2, title: "Board", dataSources: sources }, widgets: [] },
  );
}

describe("a published link's marking ceiling", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
    api.post.mockResolvedValue({ status: "ok", items: [{ value: "m1", label: "TLP:CLEAR" }, { value: "m3", label: "TLP:AMBER" }], code: null });
    api.patch.mockResolvedValue({ ...LINK, marking_ceiling: ["TLP:AMBER"] });
  });

  it("is picked among the integration's markings, by name", async () => {
    serve({ cti: { type: "platform", options: { source: "opencti", integration_id: "i1", native: {} } } });
    renderWithProviders(<DashboardPublicLinkSection dashboardId={ID} open />);
    const ceiling = await screen.findByTestId("dashboard.share.publicLink.ceiling");
    await waitFor(() =>
      expect(api.post.mock.calls.find(([url]) => url === "/custom-dashboards/platform-schema")?.[1]).toEqual({
        source: "opencti", integration_id: "i1", kind: "values", key: "objectMarking", perspective: "entities", search: "",
      }), // prettier-ignore
    );
    await userEvent.setup().click(within(ceiling).getByRole("combobox", { name: "No ceiling: everything the publisher sees" }));
    await userEvent.setup().click(await screen.findByRole("option", { name: "TLP:AMBER" }));
    await waitFor(() => expect(api.patch.mock.calls).toEqual([[`/custom-dashboards/${ID}/public-link`, { marking_ceiling: ["TLP:AMBER"] }]]));
  });

  it("is not offered for a dashboard that reads no OpenCTI", async () => {
    serve({ n: { type: "static", options: { shape: "number", data: { value: 1 } } } });
    renderWithProviders(<DashboardPublicLinkSection dashboardId={ID} open />);
    expect(await screen.findByText("Read again now")).toBeTruthy();
    expect(screen.queryByTestId("dashboard.share.publicLink.ceiling")).toBeNull();
  });
});
