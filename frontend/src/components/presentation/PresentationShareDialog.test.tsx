// Sharing a deck after the public link moved into the shared PublicLinkCard: the PDF
// download is its option, switched at once for the deck; the link is its managers' alone.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Presentation } from "@/types";
import { PresentationShareDialog } from "./PresentationShareDialog";

const { update, setPdf, link } = vi.hoisted(() => ({
  update: vi.fn(),
  setPdf: vi.fn(),
  link: { data: undefined as unknown },
}));

vi.mock("@/hooks/api/usePresentationApi", () => ({
  usePresentationPublicLink: () => ({ data: link.data, isLoading: false, isError: false }),
  useEnablePresentationPublicLink: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDisablePresentationPublicLink: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdatePresentationPublicLink: () => ({ mutateAsync: setPdf, isPending: false }),
  useUpdatePresentation: () => ({ mutateAsync: update, isPending: false }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/components/ui/principal-sharing-card", () => ({
  PrincipalSharingCard: ({ onChange }: { onChange: (v: string[]) => void }) => (
    <button type="button" onClick={() => onChange(["g-1"])}>
      Share with Marketing
    </button>
  ),
}));

const DECK = {
  id: "p-1",
  user_id: "u-1",
  title: "Weekly brief",
  is_company_managed: false,
  access_restricted: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  shared_user_ids: null,
  shared_user_permissions: null,
  can_manage: true,
} as unknown as Presentation;

function share(deck: Presentation = DECK) {
  renderWithProviders(<PresentationShareDialog presentation={deck} open onOpenChange={() => {}} />);
}

beforeEach(() => {
  update.mockReset().mockResolvedValue({});
  setPdf.mockReset().mockResolvedValue({});
  link.data = {
    available: true,
    enabled: true,
    path: "/public/presentations/tok",
    created_at: null,
    created_by_name: null,
    allow_pdf_download: false,
  };
});

describe("PresentationShareDialog", () => {
  it("switches the link's PDF download at once, for this deck", async () => {
    share();
    expect(screen.getByText("Visitors can also download the slides as a PDF, without speaker notes.")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("presentation.share.publicLink.pdf"));
    await waitFor(() => expect(setPdf).toHaveBeenCalledWith({ id: "p-1", allowPdfDownload: true }));
    expect(await screen.findByText("PDF download allowed")).toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });

  it("saves the grants the dialog changed", async () => {
    share();
    fireEvent.click(screen.getByRole("button", { name: "Share with Marketing" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0][0]).toMatchObject({ id: "p-1", is_company_managed: false, shared_group_ids: ["g-1"] });
  });

  it("offers a reader no public link", () => {
    share({ ...DECK, can_manage: false } as Presentation);
    expect(screen.queryByTestId("presentation.share.publicLink")).toBeNull();
  });
});
