// Sharing a video: only the sharing fields travel, never the spec; the public link is its
// managers' alone and says what its download option is - a convenience, not a protection.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { VideoProject } from "@/types";
import { VideoShareDialog } from "./VideoShareDialog";

const { updateSharing, enable, link } = vi.hoisted(() => ({
  updateSharing: vi.fn(),
  enable: vi.fn(),
  link: { data: undefined as unknown },
}));

vi.mock("@/hooks/api/useVideoStudioApi", () => ({
  useVideoPublicLink: () => ({ data: link.data, isLoading: false, isError: false }),
  useEnableVideoPublicLink: () => ({ mutateAsync: enable, isPending: false }),
  useDisableVideoPublicLink: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateVideoPublicLink: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateVideoSharing: () => ({ mutateAsync: updateSharing, isPending: false }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
// The people and groups picker has its own tests: here it grants one group when asked.
vi.mock("@/components/ui/principal-sharing-card", () => ({
  PrincipalSharingCard: ({ onChange, onPermissionsChange }: { onChange: (v: string[]) => void; onPermissionsChange: (v: Record<string, string>) => void }) => (
    <button
      type="button"
      onClick={() => {
        onChange(["g-1"]);
        onPermissionsChange({ "g-1": "config" });
      }}
    >
      Share with Marketing
    </button>
  ),
}));

const VIDEO = {
  id: "v-1",
  user_id: "u-1",
  title: "Launch film",
  spec: { title: "Launch film", scenes: [{ type: "title", title: "Launch" }] },
  is_company_managed: false,
  access_restricted: false,
  shared_group_ids: [],
  shared_group_permissions: null,
  shared_user_ids: [],
  shared_user_permissions: null,
  can_manage: true,
  ready_render: null,
} as unknown as VideoProject;

function share(video: VideoProject = VIDEO) {
  renderWithProviders(<VideoShareDialog video={video} open onOpenChange={() => {}} />);
}

beforeEach(() => {
  updateSharing.mockReset().mockResolvedValue({});
  enable.mockReset().mockResolvedValue({});
  link.data = { available: true, enabled: false, path: null, created_at: null, created_by_name: null, allow_download: false };
});

describe("VideoShareDialog", () => {
  it("saves the sharing fields alone, never the spec, once something changed", async () => {
    share();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Share with Marketing" }));
    expect(screen.getByText("Applies when you save.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateSharing).toHaveBeenCalledTimes(1));
    const sent = updateSharing.mock.calls[0][0];
    expect(sent).toMatchObject({ projectId: "v-1", is_company_managed: false, shared_group_ids: ["g-1"], shared_group_permissions: { "g-1": "config" } });
    expect(sent).not.toHaveProperty("spec");
    expect(sent).not.toHaveProperty("title");
  });

  it("offers its managers the public link, and says the download option protects nothing", async () => {
    link.data = { available: true, enabled: true, path: "/public/videos/tok", created_at: null, created_by_name: null, allow_download: false };
    share();
    expect(screen.getByTestId("video.share.publicLink.download")).toBeInTheDocument();
    expect(screen.getByText(/It is a convenience, not a protection: anyone who can watch the video can save what it plays\./)).toBeInTheDocument();
    // Turned on but never rendered: the link says so until the first render.
    expect(screen.getByText("The video has not been rendered yet: the link says so until its first render is ready.")).toBeInTheDocument();
  });

  it("never shows the public link to someone who can only view the video", () => {
    share({ ...VIDEO, can_manage: false });
    expect(screen.queryByTestId("video.share.publicLink")).toBeNull();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});
