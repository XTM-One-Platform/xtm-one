// The workspace groups of the chat sidebar.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { SidebarWorkspaces } from "@/pages/chat/useSidebarWorkspaces";
import type { Workspace } from "@/types";
import { UnfiledSection, WorkspaceGroupSection } from "./WorkspaceGroups";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: false }),
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const put = vi.mocked(api.put);
const remove = vi.mocked(api.delete);

const sidebar = {
  fileConversation: vi.fn(),
  renameWorkspace: vi.fn(),
  toggle: vi.fn(),
  setGroupOpen: vi.fn(),
  expanded: new Set<string>(),
} as unknown as SidebarWorkspaces;

function workspace(over: Partial<Workspace> = {}): Workspace {
  return {
    id: "w-1",
    user_id: "me",
    name: "PX-17 phishing",
    slug: "px-17-phishing",
    description: "",
    icon: "",
    color: "",
    is_default: false,
    is_archived: false,
    is_company_managed: false,
    shared_group_ids: null,
    shared_group_permissions: null,
    shared_user_ids: null,
    shared_user_permissions: null,
    access_restricted: false,
    can_manage: true,
    instructions: "",
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...over,
  };
}

function Group({ ws }: { ws: Workspace }) {
  return (
    <MemoryRouter>
      <WorkspaceGroupSection
        sidebar={sidebar}
        workspace={ws}
        groupKey={ws.id}
        count={0}
        open
        onNewConversation={vi.fn()}
        canCreateConversation
      >
        <p>rows</p>
      </WorkspaceGroupSection>
    </MemoryRouter>
  );
}

function renderGroup(ws: Workspace) {
  return renderWithProviders(<Group ws={ws} />);
}

/** A promise that never settles: a request still in flight. */
function pending<T>(): Promise<T> {
  return new Promise<T>(() => {});
}

function openMenu() {
  // The design system's menu (Radix's dropdown) opens on the primary button's
  // `pointerdown`, the first half of a real click, not on `click` itself.
  fireEvent.pointerDown(screen.getByRole("button", { name: "Workspace actions" }), { button: 0, ctrlKey: false });
  return screen.getByRole("menu");
}

function item(name: string) {
  return within(screen.getByRole("menu")).getByRole("menuitem", { name }) as HTMLButtonElement;
}

/** Whether a menu item is disabled, as assistive technology reads it: a
 *  library menu item is a `menuitem` element marked `aria-disabled`, not a
 *  native button with a `disabled` property. */
function isDisabled(el: HTMLElement): boolean {
  return el.getAttribute("aria-disabled") === "true" || (el as HTMLButtonElement).disabled === true;
}

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (url: string) => {
    if (url === "/workspaces/w-1/knowledge-bases") {
      return [{ knowledge_base_id: "kb-1", name: "Threat intel", description: "" }];
    }
    if (url.startsWith("/knowledge-bases")) return [{ id: "kb-1", name: "Threat intel" }];
    return [];
  });
  post.mockResolvedValue({});
  put.mockResolvedValue({});
  remove.mockResolvedValue(undefined);
});

describe("WorkspaceGroupSection actions menu", () => {
  it("offers the Workspaces card's actions, in its order", () => {
    renderGroup(workspace());

    const names = within(openMenu())
      .getAllByRole("menuitem")
      .map((el) => el.textContent);
    expect(names).toEqual([
      "Open workspace",
      "Rename",
      "Make default",
      "Knowledge bases",
      "Share",
      "Archive",
      "Delete",
    ]);
  });

  it("renames in place from the menu", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Rename"));

    expect(await screen.findByDisplayValue("PX-17 phishing")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("binds the workspace's knowledge bases from the chat", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Knowledge bases"));

    const dialog = await screen.findByRole("dialog");
    fireEvent.click(await within(dialog).findByRole("button", { name: "Remove Threat intel" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]).toEqual(["/workspaces/w-1/knowledge-bases", { knowledge_base_ids: [] }]);
  });

  it("keeps Save disabled while the bindings are still loading", async () => {
    get.mockImplementation((url: string) =>
      url === "/workspaces/w-1/knowledge-bases" ? pending() : Promise.resolve([]),
    );
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Knowledge bases"));

    const dialog = await screen.findByRole("dialog");
    const save = within(dialog).getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(save);
    expect(put).toHaveBeenCalledTimes(0);
  });

  it("keeps Save disabled and says so when the bindings could not be read", async () => {
    get.mockImplementation((url: string) =>
      url === "/workspaces/w-1/knowledge-bases"
        ? Promise.reject(new Error("boom"))
        : Promise.resolve([{ id: "kb-1", name: "Threat intel" }]),
    );
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Knowledge bases"));

    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText("Could not load the knowledge bases bound to this workspace."),
    ).toBeTruthy();
    expect((within(dialog).getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(put).toHaveBeenCalledTimes(0);
  });

  it("opens the share dialog from the chat", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Share"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Share workspace")).toBeTruthy();
  });

  it("compares the share draft with the workspace as it was when the dialog opened", async () => {
    const { rerender } = renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Share"));
    const dialog = await screen.findByRole("dialog");
    const save = () => within(dialog).getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save().disabled).toBe(true);

    // The list refetches while the dialog is open: a colleague shared it.
    rerender(<Group ws={workspace({ shared_group_ids: ["g-colleague"], shared_group_permissions: { "g-colleague": "read_only" } })} />);

    expect(save().disabled).toBe(true);
    expect(put).toHaveBeenCalledTimes(0);
  });

  it("makes it the default workspace", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Make default"));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]).toEqual(["/workspaces/w-1/default"]);
  });

  it("archives it once confirmed", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Archive"));

    const confirm = await screen.findByRole("dialog");
    expect(within(confirm).getByText("Archive this workspace?")).toBeTruthy();
    expect(put).toHaveBeenCalledTimes(0);
    fireEvent.click(within(confirm).getByRole("button", { name: "Archive" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]).toEqual(["/workspaces/w-1", { is_archived: true }]);
  });

  it("does not archive it when the confirmation is cancelled", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Archive"));

    const confirm = await screen.findByRole("dialog");
    fireEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(put).toHaveBeenCalledTimes(0);
  });

  it("restores an archived workspace without asking", async () => {
    renderGroup(workspace({ is_archived: true }));
    openMenu();
    fireEvent.click(item("Restore"));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]).toEqual(["/workspaces/w-1", { is_archived: false }]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the API's reason when an action is refused", async () => {
    post.mockRejectedValue(new Error("This workspace cannot be your default."));
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Make default"));

    expect(await screen.findByText("This workspace cannot be your default.")).toBeTruthy();
  });

  it("deletes it once confirmed", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Delete"));

    const confirm = await screen.findByRole("dialog");
    // The sidebar does not read the work-item counts: the confirmation says the rule.
    expect(
      within(confirm).getByText(
        "Its conversations stay, in no workspace, and bound knowledge bases are left untouched. A workspace that still holds work items cannot be deleted: archive it instead.",
      ),
    ).toBeTruthy();
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
    expect(remove.mock.calls[0]).toEqual(["/workspaces/w-1"]);
  });

  it("does not delete it when the confirmation is cancelled", async () => {
    renderGroup(workspace());
    openMenu();
    fireEvent.click(item("Delete"));

    const confirm = await screen.findByRole("dialog");
    fireEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(remove).toHaveBeenCalledTimes(0);
  });

  it("disables every change on a workspace shared read-only, keeps the way to the workspace", () => {
    renderGroup(workspace({ user_id: "someone-else", can_manage: false }));
    openMenu();

    for (const name of ["Rename", "Make default", "Knowledge bases", "Share", "Archive", "Delete"]) {
      expect(isDisabled(item(name))).toBe(true);
    }
    expect(isDisabled(item("Open workspace"))).toBe(false);
  });

  it.each([
    ["archived", { is_archived: true }],
    ["company-managed", { is_company_managed: true, user_id: null }],
    ["someone else's, shared with Can configure", { user_id: "someone-else" }],
  ])("offers no Make default on a workspace that is %s", (_case, over) => {
    renderGroup(workspace(over as Partial<Workspace>));
    openMenu();

    expect(isDisabled(item("Make default"))).toBe(true);
    expect(isDisabled(item("Share"))).toBe(false);
  });

  it("keeps the default workspace from being made default, archived or deleted", () => {
    renderGroup(workspace({ is_default: true }));
    openMenu();

    expect(isDisabled(item("Make default"))).toBe(true);
    expect(isDisabled(item("Archive"))).toBe(true);
    expect(isDisabled(item("Delete"))).toBe(true);
    expect(isDisabled(item("Knowledge bases"))).toBe(false);
    expect(isDisabled(item("Share"))).toBe(false);
  });
});

describe("UnfiledSection", () => {
  it("starts a conversation filed in no workspace from its header", () => {
    const onNewConversation = vi.fn();
    renderWithProviders(
      <UnfiledSection sidebar={sidebar} count={0} onNewConversation={onNewConversation} canCreateConversation>
        <p>rows</p>
      </UnfiledSection>,
    );

    fireEvent.click(screen.getByRole("button", { name: "New conversation outside a workspace" }));
    expect(onNewConversation).toHaveBeenCalledTimes(1);
    expect(onNewConversation.mock.calls[0]).toEqual([]);
  });

  it("offers no new conversation while chat is unavailable", () => {
    renderWithProviders(
      <UnfiledSection sidebar={sidebar} count={0} onNewConversation={vi.fn()} canCreateConversation={false}>
        <p>rows</p>
      </UnfiledSection>,
    );

    expect(screen.queryByRole("button", { name: "New conversation outside a workspace" })).toBeNull();
  });
});
