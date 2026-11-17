// Render retention edit: an out-of-range day count is flagged on the field itself
// (aria-invalid, an inline error tied by aria-describedby), not only by a disabled Save.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/api/useVideoStudioApi", () => ({
  useRenderRetention: () => ({ data: { failed_cancelled_days: 1, previews_days: 7, superseded_ready_days: 30 }, isLoading: false }),
  useSaveRenderRetention: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { RenderRetentionSection } from "./RenderRetentionSection";

describe("RenderRetentionSection edit", () => {
  it("marks an invalid day count and says why", async () => {
    renderWithProviders(<RenderRetentionSection isAdmin />);
    await userEvent.setup().click(screen.getByRole("button", { name: /edit/i }));
    const field = screen.getByLabelText("Scene previews");
    expect(field).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.change(field, { target: { value: "-3" } });
    expect(field).toHaveAttribute("aria-invalid", "true");
    const error = screen.getByRole("alert");
    expect(error).toHaveTextContent("Enter a whole number from 0 to 3650");
    expect(field).toHaveAttribute("aria-describedby", error.id);

    fireEvent.change(field, { target: { value: "14" } });
    expect(field).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
