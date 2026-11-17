// The Workspaces tab on the Operations row contract: the row keeps one line
// of contents, the hover card names every fact of the workspace whole. jsdom
// does no layout: the row height and the column alignment are checked
// rendered (tests_e2e/operations-list-grid.spec.ts).

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import type { Workspace } from "@/types";
import { workspaceContentsLine, workspaceFacts } from "./WorkspacesTab";

const t = (id: string, values?: Record<string, string | number>) =>
  id.replace(/\{(\w+)\}/g, (_, k: string) => String(values?.[k] ?? ""));

const EMPTY = { conversations: 0, work_items: 0, held_work_items: 0, open_work_items: 0, shared_states: 0, worksets: 0, knowledge_bases: 0 };

const ws = (overrides: Partial<Workspace>): Workspace => ({
  id: "w1",
  user_id: "u1",
  owner_name: "Owner Name",
  name: "Workspace",
  slug: "workspace",
  description: "",
  icon: "",
  color: "",
  is_default: false,
  is_archived: false,
  is_company_managed: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  can_manage: true,
  instructions: "",
  created_at: "2026-11-04T10:00:00Z",
  updated_at: "2026-11-04T11:00:00Z",
  ...overrides,
});

const labels = (w: Workspace, viewer = { id: "u1", isAdmin: false }) =>
  workspaceFacts(w, viewer, t, (iso) => iso).map((f) => f.label);

const text = (w: Workspace, label: string) => {
  const fact = workspaceFacts(w, { id: "u1", isAdmin: false }, t, (iso) => iso).find((f) => f.label === label);
  return typeof fact?.value === "string" ? fact.value : renderToStaticMarkup(<>{fact?.value}</>).replace(/<[^>]+>/g, "");
};

describe("workspaceContentsLine", () => {
  it("says nothing when nobody counted", () => {
    expect(workspaceContentsLine(undefined, false, t)).toBeNull();
    expect(workspaceContentsLine(null, false, t)).toBeNull();
  });

  it("says an empty workspace is empty in words, unless it hosts a fleet", () => {
    expect(workspaceContentsLine(EMPTY, false, t)).toEqual({ text: "Nothing filed here yet.", open: null });
    expect(workspaceContentsLine(EMPTY, true, t)).toBeNull();
  });

  it("names every non-zero count on one line and the live work apart", () => {
    const line = workspaceContentsLine({ ...EMPTY, conversations: 1, work_items: 3, open_work_items: 2, worksets: 2 }, false, t);
    expect(line).toEqual({ text: "1 conversation · 3 work items · 2 worksets", open: "(2 still open)" });
  });
});

describe("workspaceFacts", () => {
  it("lists the facts in the contract's order: owner, fleet, state, access, description, contents, slug, timestamps", () => {
    const w = ws({
      user_id: "u2",
      is_archived: true,
      description: "What it is for",
      contents: { ...EMPTY, work_items: 2 },
      hosted_fleets: [{ id: "f1", name: "A fleet", slug: "a-fleet", is_archived: false }],
      hosted_fleet_count: 1,
      shared_user_ids: ["u1"],
      can_manage: false,
    });
    expect(labels(w)).toEqual(["Owner", "Fleet", "Status", "Access", "Description", "Contents (what it holds)", "Slug", "Created", "Updated"]);
  });

  it("names the viewer as You, and the default only on the owner's own workspace", () => {
    expect(text(ws({ is_default: true }), "Owner")).toBe("You");
    expect(text(ws({ is_default: true }), "Status")).toBe("Default");
    expect(labels(ws({ is_default: true, user_id: "u2" }))).not.toContain("Status");
  });

  it("counts the fleets the viewer cannot see without naming them", () => {
    const w = ws({
      hosted_fleets: [{ id: "f1", name: "A fleet", slug: "a-fleet", is_archived: true }],
      hosted_fleet_count: 3,
    });
    expect(text(w, "Fleets")).toBe("A fleet (archived), and 2 fleets you cannot see");
  });

  it("gives a long slug and a long description whole", () => {
    const slug = "operations-lists-best-in-class-with-a-very-long-suffix";
    const description = "A description long enough that no row can hold it whole, given whole in the hover card.";
    const w = ws({ slug, description });
    expect(text(w, "Slug")).toBe(slug);
    expect(text(w, "Description")).toBe(description);
  });

  it("carries the open work as a warning in the contents fact", () => {
    const fact = workspaceFacts(ws({ contents: { ...EMPTY, work_items: 4, open_work_items: 1 } }), { id: "u1", isAdmin: false }, t, (iso) => iso)
      .find((f) => f.label === "Contents (what it holds)");
    expect(fact?.value).toBe("4 work items (1 still open)");
    expect(fact?.tone).toBe("warning");
  });

  it("says a company-managed workspace is the administrators' to manage", () => {
    expect(text(ws({ is_company_managed: true, user_id: null }), "Access")).toBe(
      "Only an administrator can manage a company-managed entry.",
    );
  });
});
