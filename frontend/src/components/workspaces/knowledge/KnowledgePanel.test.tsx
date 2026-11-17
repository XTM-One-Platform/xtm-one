// The knowledge panel of a workspace.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Workspace, WorkspaceKnowledgeSummary } from "@/types";
import { KnowledgePanel } from "./KnowledgePanel";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: false }),
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const put = vi.mocked(api.put);

const WORKSPACE = {
  id: "w-1",
  user_id: "me",
  name: "Launch",
  slug: "launch",
  description: "",
  icon: "",
  color: "",
  is_default: false,
  is_archived: false,
  is_company_managed: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  can_manage: true,
  instructions: "Answer in French.",
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
} satisfies Workspace;

function summary(over: Partial<WorkspaceKnowledgeSummary> = {}): WorkspaceKnowledgeSummary {
  return {
    instructions: "Answer in French.",
    can_manage: true,
    sources_knowledge_base_id: null,
    sources: { count: 4, ready: 2, pending: 0, failed: 1 },
    skills: 1,
    knowledge_bases: 1,
    conversations: { indexed: 3, pending: 2 },
    ...over,
  };
}

function serve(knowledge: WorkspaceKnowledgeSummary) {
  get.mockImplementation(async (url: string) => {
    if (url === "/workspaces/w-1") return WORKSPACE;
    if (url === "/workspaces/w-1/knowledge") return knowledge;
    if (url === "/workspaces/w-1/knowledge-bases") return [{ knowledge_base_id: "kb-9", name: "Threat intel", description: "" }];
    if (url === "/workspaces/w-1/skills") return [{ skill_id: "s-1", name: "Phishing triage", description: "" }];
    if (url.startsWith("/skills")) return [{ id: "s-1", name: "Phishing triage" }];
    return [];
  });
}

function renderPanel() {
  return renderWithProviders(
    <MemoryRouter>
      <KnowledgePanel workspaceId="w-1" />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  post.mockResolvedValue({ knowledge_base_id: "kb-src" });
  put.mockResolvedValue({});
});

describe("KnowledgePanel", () => {
  it("lets a manager edit the instructions and start the sources", async () => {
    serve(summary());
    renderPanel();

    const field = await screen.findByRole("textbox", { name: "Instructions" });
    fireEvent.change(field, { target: { value: "Answer in English." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/workspaces/w-1", { instructions: "Answer in English." }));

    fireEvent.click(screen.getByRole("button", { name: "Add sources" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/workspaces/w-1/sources"));
    expect(screen.getByRole("button", { name: "Manage" })).toBeTruthy();
  });

  it("shows a reader the same knowledge with no control", async () => {
    serve(summary({ can_manage: false }));
    renderPanel();

    expect(await screen.findByText("Answer in French.")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Instructions" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add sources" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Manage" })).toBeNull();
    expect(screen.getByText("No sources yet.")).toBeTruthy();
    expect(await screen.findByText("Threat intel")).toBeTruthy();
    expect(await screen.findByText("Phishing triage")).toBeTruthy();
  });

  it("renders the source statuses and the conversation index counts", async () => {
    serve(summary({ can_manage: false }));
    renderPanel();

    expect(await screen.findByText("2 ready")).toBeTruthy();
    expect(screen.getByText("1 failed")).toBeTruthy();
    expect(screen.queryByText(/processing/)).toBeNull();
    expect(screen.getByText("3 searchable")).toBeTruthy();
    expect(screen.getByText("2 waiting")).toBeTruthy();
  });
});
