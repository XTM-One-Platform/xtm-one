// Settings > Storage & retention (#396): every category with its days, its
// basis and its lock; a shorter value needs a Preview before Save.
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { FileRetentionSection } from "@/pages/settings/FileRetentionSection";
import { renderWithProviders } from "@/testing/providers";

const { get, put, post } = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), api: { get, put, post } }));

const row = (category: string, days: number, extra: object = {}) => ({
  category,
  days,
  never: days === 0,
  basis: category.startsWith("agent_") && category !== "agent_check" ? "last_use" : "created",
  source: "default",
  locked: false,
  default_days: days,
  env_var: `FILE_RETENTION_${category.toUpperCase()}_DAYS`,
  unit: "days",
  files: 10,
  bytes: 1024 * 1024,
  deleted: 0,
  ...extra,
});

const settings = {
  categories: [
    row("agent_working", 60),
    row("agent_download", 60),
    row("chat", 0),
    row("agent_check", 7, { locked: true, source: "environment", env_var: "AGENT_CHECK_FILE_RETENTION_DAYS" }),
  ],
  max_days: 3650,
  next_sweep_at: "2026-11-19T12:00:00Z",
  last_sweep: { at: "2026-11-19T11:00:00Z", files: { agent_working: 4 }, bytes: { agent_working: 4096 } },
  can_edit: true,
};

afterEach(cleanup);

beforeEach(() => {
  get.mockReset().mockResolvedValue(settings);
  put.mockReset().mockResolvedValue(settings);
  post.mockReset().mockResolvedValue({
    categories: [{ category: "agent_working", files: 3, bytes: 3 * 1024 * 1024, kept_referenced: 1, more: false }],
    total_files: 3,
    total_bytes: 3 * 1024 * 1024,
  });
});

describe("FileRetentionSection", () => {
  it("lists every category with its days, its basis and its lock", async () => {
    renderWithProviders(<FileRetentionSection isAdmin />);
    const working = await screen.findByTestId("settings.file-retention.row.agent_working");
    expect(working).toHaveTextContent("Agent working files");
    expect(working).toHaveTextContent("60 days");
    expect(working).toHaveTextContent("after the last use");
    expect(screen.getByTestId("settings.file-retention.row.chat")).toHaveTextContent("Never");
    expect(screen.getByTestId("settings.file-retention.row.agent_check")).toHaveTextContent(
      "Set by AGENT_CHECK_FILE_RETENTION_DAYS",
    );
  });

  it("asks for a preview before saving a shorter retention", async () => {
    renderWithProviders(<FileRetentionSection isAdmin />);
    await screen.findByTestId("settings.file-retention.row.agent_working");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    // A locked category has no field.
    expect(screen.queryByLabelText("Check pictures")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Agent working files"), { target: { value: "30" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/settings/file-retention/preview", { days: { agent_working: 30 } }));
    expect(await screen.findByTestId("settings.file-retention.preview-total")).toHaveTextContent("3 files");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/settings/file-retention", { days: { agent_working: 30 } }));
  });

  it("saves a longer retention without a preview", async () => {
    renderWithProviders(<FileRetentionSection isAdmin />);
    await screen.findByTestId("settings.file-retention.row.agent_working");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Agent deliverables"), { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/settings/file-retention", { days: { agent_download: 90 } }));
    expect(post).not.toHaveBeenCalled();
  });

  it("tells a non-administrator the section is theirs to read only", () => {
    renderWithProviders(<FileRetentionSection isAdmin={false} />);
    expect(screen.getByTestId("settings.file-retention.admin-only")).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
