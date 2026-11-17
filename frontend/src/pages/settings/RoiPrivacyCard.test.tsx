// Settings > Organization > Rates by role: "Value visibility and privacy" (#12077 L6b, design 12.5):
// the read view at rest, the three fields on Edit, the k bounds the server checks; a refused Save
// focuses the k field (UX r1 B1 on #12155).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { parseMinGroupSize } from "@/lib/roi-settings";
import { RoiPrivacyCard } from "./RoiPrivacyCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);

const STORED = { roi_org_totals_visible: false, roi_min_group_size: 5, roi_by_person_visible: false };

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => {
    if (path === "/roi/settings") return STORED;
    throw new Error(`unexpected GET ${path}`);
  });
  put.mockImplementation(async (_path: string, body: unknown) => ({ ...STORED, ...(body as object) }));
});
afterEach(cleanup);

describe("parseMinGroupSize", () => {
  it("accepts a whole number from 2 to 50 only", () => {
    expect(parseMinGroupSize("5")).toBe(5);
    expect(parseMinGroupSize(" 2 ")).toBe(2);
    expect(parseMinGroupSize("50")).toBe(50);
    expect(parseMinGroupSize("1")).toBeNull();
    expect(parseMinGroupSize("51")).toBeNull();
    expect(parseMinGroupSize("2.5")).toBeNull();
    expect(parseMinGroupSize("")).toBeNull();
  });
});

describe("RoiPrivacyCard", () => {
  it("reads the three settings as words at rest, never a disabled switch", async () => {
    renderWithProviders(<RoiPrivacyCard />);
    const card = await screen.findByTestId("settings.roi.privacy");
    await waitFor(() => expect(card.textContent).toContain("5 people"));
    expect(card.textContent).toContain("Organization totals for every user");
    expect(card.textContent).toContain("By person breakdown for administrators");
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("saves the three fields in one PUT", async () => {
    renderWithProviders(<RoiPrivacyCard />);
    const edit = await screen.findByRole("button", { name: "Edit value visibility and privacy" });
    await waitFor(() => expect((edit as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(edit);
    fireEvent.click(await screen.findByRole("switch", { name: /Organization totals for every user/ }));
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/roi/settings", {
        roi_org_totals_visible: true,
        roi_min_group_size: 8,
        roi_by_person_visible: false,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("switch")).toBeNull());
  });

  it("refuses a k out of bounds with an invalid field and sends nothing", async () => {
    renderWithProviders(<RoiPrivacyCard />);
    const edit = await screen.findByRole("button", { name: "Edit value visibility and privacy" });
    await waitFor(() => expect((edit as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(edit);
    const field = await screen.findByRole("spinbutton");
    fireEvent.change(field, { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("A whole number from 2 to 50.")).toBeTruthy();
    expect(field.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect(document.activeElement).toBe(field));
    expect(put).not.toHaveBeenCalled();
  });

  it("keeps a failed read in its own line", async () => {
    get.mockRejectedValue(new Error("boom"));
    renderWithProviders(<RoiPrivacyCard />);
    expect(await screen.findByTestId("settings.roi.privacy.error")).toBeTruthy();
  });

  it("reads the k of 1 in the singular", async () => {
    get.mockResolvedValue({ ...STORED, roi_min_group_size: 1 });
    renderWithProviders(<RoiPrivacyCard />);
    const card = await screen.findByTestId("settings.roi.privacy");
    await waitFor(() => expect(card.textContent).toContain("1 person"));
  });
});
