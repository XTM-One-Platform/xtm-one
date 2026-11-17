// The Memory tab: one data table of the agent's memory (#9378 slice 4f). The API
// pages, filters, searches and sorts; an entry names the object it describes, one
// that no longer holds is marked superseded, and a row opens in place on the whole
// entry, rendered.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { HOSTILE_URLS } from "@/testing/hostile-urls";
import type { MemoryListParams } from "@/hooks/api/useMemoryApi";
import type { Agent, AgentMemoryEntry } from "@/types";
import { MEMORY_SOURCE_LABELS, MEMORY_TYPE_CONFIG } from "@/lib/constants";
import { MemoryTab } from "./MemoryTab";

// Every memory_type of the API type and every source the backend writes
// (backend/app/services/persistent_memory.py, agent_memory.py, the agent tools,
// assignments, style learning) has a label: a raw value never reaches the filter
// or the cell (UX round 1 on #9975, B1 and B2).
const MEMORY_TYPE_VALUES: AgentMemoryEntry["memory_type"][] = ["consolidated", "fact", "learned", "style", "preference", "state", "tradecraft"];
const BACKEND_SOURCES = ["chat", "compaction", "consolidation", "reconciliation", "assignment", "learning", "tool"];

const base = {
  agent_id: "agent-1",
  user_id: "user-1",
  memory_type: "state",
  source: "tool",
  updated_at: null,
  subject_key: "doc:X",
} as const;

const defaultEntries: AgentMemoryEntry[] = [
  { ...base, id: "final", content: "doc X: footer reads Final", created_at: "2026-10-11T20:05:00Z", superseded: false },
  {
    ...base,
    id: "draft",
    content: "doc X: footer reads Draft",
    created_at: "2026-10-11T19:00:00Z",
    superseded: true,
    superseded_by_id: "final",
  },
  { ...base, id: "fact", memory_type: "fact", subject_key: null, content: "The team ships on Thursdays.", created_at: "2026-10-10T08:00:00Z" },
];
let entries: AgentMemoryEntry[] = defaultEntries;
let listError = false;
const listCalls: MemoryListParams[] = [];
const refetch = vi.fn();

const idle = { mutate: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useMemoryApi", () => ({
  useAgentMemories: (_id: string, params: MemoryListParams) => {
    listCalls.push(params);
    return listError
      ? { data: undefined, isLoading: false, isError: true, refetch }
      : { data: { entries, total_count: entries.length }, isLoading: false, isError: false, refetch };
  },
  useSearchAgentMemory: () => ({ data: undefined, isFetching: false, isError: false, refetch }),
  useUpdateMemoryEntry: () => idle,
  useDeleteMemoryEntry: () => idle,
  useClearAgentMemory: () => idle,
  useAgentMemoryHealth: () => ({ data: undefined }),
  useTriggerMemoryMaintenance: () => idle,
}));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/hooks/useAiDisabled", () => ({ useAiActionDisabled: () => false, useAiDisabledReason: () => null }));

const agent = { id: "agent-1", memory_persistent_enabled: true } as Agent;

function show(canManage = false, route = "/agents/agent-1/memory") {
  return renderWithProviders(
    <MemoryRouter initialEntries={[route]}>
      <MemoryTab agent={agent} canManage={canManage} />
    </MemoryRouter>,
  );
}
/** The table's own query: the one that asks for a page (the cards ask for one row). */
const tableCall = () => listCalls.filter((params) => params.limit !== 1).slice(-1)[0];

afterEach(() => {
  entries = defaultEntries;
  listError = false;
  listCalls.length = 0;
});

describe("MemoryTab", () => {
  it("names each entry's subject and marks the one that no longer holds", () => {
    show();

    expect(screen.getAllByText("doc:X")).toHaveLength(2);
    const rows = screen.getAllByTestId("memory-row");
    expect(rows).toHaveLength(3);
    expect(rows[1].textContent).toContain("doc X: footer reads Draft");
    expect(rows[1].textContent).toContain("Superseded");
    expect(rows[0].textContent).not.toContain("Superseded");
  });

  it("asks the API for the first page, newest first", () => {
    show();

    expect(tableCall()).toMatchObject({ sort: "date", direction: "desc", limit: 25, offset: 0 });
  });

  it("sends the URL's search, filters and sort to the API", () => {
    show(false, "/agents/agent-1/memory?mem_q=footer&mem_f_memory_type=state&mem_f_memory_type=fact&mem_f_state=current&mem_sort=memory_type:asc&mem_page=2");

    expect(tableCall()).toMatchObject({
      q: "footer",
      memory_type: ["state", "fact"],
      state: "current",
      sort: "type",
      direction: "asc",
      offset: 25,
    });
  });

  it("opens a row on the whole entry, rendered", () => {
    entries = [{ ...base, id: "long", subject_key: null, content: "First line\n\n**Second** paragraph", created_at: "2026-10-10T08:00:00Z" }];
    show();

    expect(screen.queryByText("Second")).toBeNull();
    fireEvent.click(screen.getByText("First line"));
    expect(screen.getByText("Second").tagName).toBe("STRONG");
  });

  it("opens a row from the keyboard", () => {
    entries = [{ ...base, id: "long", subject_key: null, content: "First line\n\n**Second** paragraph", created_at: "2026-10-10T08:00:00Z" }];
    show();

    const row = screen.getByTestId("memory-row");
    expect(row.getAttribute("aria-expanded")).toBe("false");
    fireEvent.keyDown(row, { key: "Enter" });
    expect(screen.getByText("Second").tagName).toBe("STRONG");
    expect(screen.getByTestId("memory-row").getAttribute("aria-expanded")).toBe("true");
  });

  it("labels every memory type and every source the backend writes", () => {
    for (const type of MEMORY_TYPE_VALUES) expect(MEMORY_TYPE_CONFIG[type]?.label, type).toBeTruthy();
    for (const source of BACKEND_SOURCES) expect(MEMORY_SOURCE_LABELS[source]?.label, source).toBeTruthy();
  });

  it("draws the table's error with Retry when the page fails", () => {
    listError = true;
    show();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalled();
  });

  it("offers edit and delete on each row to a manager", () => {
    show(true);

    expect(screen.getAllByRole("button", { name: "Edit memory" })).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: "Delete memory" })).toHaveLength(3);
  });
});

// A memory is written by the agent from what it read: a markdown link in it
// keeps its text and is followed only to a web address (`lib/safe-url.ts`).
describe("MemoryTab links", () => {
  const renderLink = (href: string) => {
    entries = [
      {
        ...base,
        id: "link",
        memory_type: "fact",
        subject_key: null,
        // An angle-bracket destination takes any character but `<` / `>`.
        content: `See [the runbook](<${href.replace(/[<>]/g, encodeURIComponent)}>) first.`,
        created_at: "2026-10-10T08:00:00Z",
      },
    ];
    const view = show();
    fireEvent.click(screen.getByTestId("memory-row"));
    return view;
  };
  const hrefs = (root: HTMLElement) => [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

  it("links a web address", () => {
    const { container } = renderLink("https://wiki.example.com/runbook");
    expect(hrefs(container)).toEqual(["https://wiki.example.com/runbook"]);
    expect(screen.getByRole("link", { name: "the runbook" })).toBeTruthy();
  });

  it.each(HOSTILE_URLS)("keeps the text of a link to %s, without its target", (_label, url) => {
    const { container } = renderLink(url);
    expect(hrefs(container)).toEqual([]);
    expect(screen.getByText("the runbook").tagName).toBe("SPAN");
  });
});
