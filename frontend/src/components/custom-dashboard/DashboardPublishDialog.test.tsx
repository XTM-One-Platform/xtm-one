// Publishing a dashboard: the only integration of the platform is implied,
// the request names the target, and the answer's link and left-out widgets
// are shown; a platform with no usable integration says so.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), download: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { DashboardPublishDialog } from "./DashboardPublishDialog";

const DASHBOARD = { id: "d1", title: "Threat board", revision: 3 } as unknown as CustomDashboard;

describe("DashboardPublishDialog", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
    api.get.mockImplementation(async (path: string) =>
      path.endsWith("/integrations") ? [{ id: "i1", provider: "opencti", name: "Lab" }] : [],
    );
  });

  it("publishes through the only integration and shows what came back", async () => {
    api.post.mockResolvedValue({
      publication: { id: "p", target: "opencti", revision: 3, remote_url: "https://cti.example/dashboard/workspaces/dashboards/ws-1" },
      notes: ["Widget 'score' (score) has no OpenCTI equivalent and was left out."],
    });
    renderWithProviders(<DashboardPublishDialog dashboard={DASHBOARD} open onOpenChange={() => {}} />);
    const publish = await screen.findByRole("button", { name: "Publish to OpenCTI" });
    await waitFor(() => expect((publish as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(publish);
    await waitFor(() =>
      expect(api.post.mock.calls).toEqual([
        ["/custom-dashboards/d1/publish", { target: "opencti", integration_id: "i1", name: "Threat board" }],
      ]),
    );
    expect((await screen.findByRole("link", { name: "Open it in OpenCTI" })).getAttribute("href")).toBe(
      "https://cti.example/dashboard/workspaces/dashboards/ws-1",
    );
    expect(screen.getByText("Widget 'score' (score) has no OpenCTI equivalent and was left out.")).toBeTruthy();
  });

  it("shares on the XTM Hub through the person's Hub integration, with a short description", async () => {
    api.get.mockImplementation(async (path: string) =>
      path.endsWith("/integrations") ? [{ id: "h1", provider: "xtm_hub", name: "Filigran Hub" }] : [],
    );
    api.post.mockResolvedValue({
      publication: { id: "p", target: "xtm_hub", revision: 3, remote_url: "http://localhost:3002/app/service/opencti_custom_dashboards/l1/d1" },
      notes: [],
    });
    renderWithProviders(<DashboardPublishDialog dashboard={DASHBOARD} open onOpenChange={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "XTM Hub" }));
    fireEvent.change(screen.getByLabelText("Short description"), { target: { value: "Weekly threat figures" } });
    const share = screen.getByRole("button", { name: "Share on XTM Hub" });
    await waitFor(() => expect((share as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(share);
    await waitFor(() =>
      expect(api.post.mock.calls).toEqual([
        [
          "/custom-dashboards/d1/share-on-hub",
          { integration_id: "h1", name: "Threat board", short_description: "Weekly threat figures" },
        ],
      ]),
    );
    expect((await screen.findByRole("link", { name: "Open it in XTM Hub" })).getAttribute("href")).toBe(
      "http://localhost:3002/app/service/opencti_custom_dashboards/l1/d1",
    );
  });

  it("sends the dashboard to OpenCTI as a custom view through an OpenCTI integration, and downloads it", async () => {
    api.post.mockResolvedValue({
      publication: { id: "p", target: "opencti-custom-view", revision: 3, remote_url: "https://cti.example/dashboard/settings/customization/entity_types/Country/custom-views/cv-1" },
      notes: [],
    });
    api.download.mockResolvedValue({ blob: new Blob(["{}"]), filename: "Threat board.json" });
    renderWithProviders(<DashboardPublishDialog dashboard={DASHBOARD} open onOpenChange={() => {}} />);
    const [segment, download] = await screen.findAllByRole("button", { name: /OpenCTI custom view/ });
    fireEvent.click(segment);
    expect(screen.getByText(/Sent as a custom view of the type of the dashboard's OpenCTI entity input/)).toBeTruthy();
    const publish = screen.getByRole("button", { name: "Publish to OpenCTI" });
    await waitFor(() => expect((publish as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(publish);
    await waitFor(() =>
      expect(api.post.mock.calls).toEqual([
        ["/custom-dashboards/d1/publish", { target: "opencti-custom-view", integration_id: "i1", name: "Threat board" }],
      ]),
    );
    fireEvent.click(download);
    await waitFor(() => expect(api.download.mock.calls).toEqual([["/custom-dashboards/d1/export?format=opencti-custom-view"]]));
  });

  it("says when the platform has no integration the person can use", async () => {
    renderWithProviders(<DashboardPublishDialog dashboard={DASHBOARD} open onOpenChange={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "OpenAEV" }));
    expect(await screen.findByText("You can use no OpenAEV integration.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Publish to OpenAEV" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
