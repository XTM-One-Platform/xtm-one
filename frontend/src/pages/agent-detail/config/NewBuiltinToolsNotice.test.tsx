import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { dismissedBuiltinFunctions, pendingAddedFunctions, withFunctionsAdded } from "@/lib/builtin-allowed-tools";
import { NewBuiltinToolsNotice } from "./NewBuiltinToolsNotice";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const GROUPS: Record<string, string[]> = { platform: ["get_agent", "list_agents", "new_tool", "other_new"], web: ["web_fetch"] };
const groupFunctions = (id: string) => GROUPS[id] ?? [];
const agent = { id: "a-1", builtin_tool_ids: ["platform", "web"], builtin_allowed_tools: { platform: ["get_agent"] } };

const render = (url: string, canManage = false, config: Record<string, unknown> | null = null) =>
  renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <NewBuiltinToolsNotice agent={{ ...agent, config }} canManage={canManage} groupFunctions={groupFunctions} />
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.mocked(api.post).mockReset().mockResolvedValue({ dismissed: [], resolved_notifications: [] });
  vi.mocked(api.put).mockReset().mockResolvedValue({});
});

describe("Dismiss wording", () => {
  // UX round 1 B1 (#12248): the row's Dismiss uses the same verb as Dismiss all
  // in every language, never the shared "Dismiss" key ("Fermer", "閉じる").
  it.each([
    ["en", en],
    ["es", es],
    ["fr", fr],
    ["ja", ja],
  ] as const)("%s: the row verb is the one Dismiss all uses", (_lang, catalog) => {
    const strings = catalog as Record<string, string>;
    const row = strings["Dismiss (built-in tool row)"];
    expect(row).toBeTruthy();
    expect(strings["Dismiss all"].toLowerCase()).toContain(row.toLowerCase());
  });
});

describe("pendingAddedFunctions", () => {
  it("keeps only functions a scoped group ships and its scope leaves out", () => {
    expect(
      pendingAddedFunctions("platform.new_tool,platform.get_agent,web.web_fetch,platform.gone,bad,platform.new_tool", agent.builtin_allowed_tools, groupFunctions),
    ).toEqual([{ groupId: "platform", name: "new_tool" }]);
    expect(pendingAddedFunctions(null, agent.builtin_allowed_tools, groupFunctions)).toEqual([]);
  });

  it("drops a function the owner dismissed", () => {
    expect(
      pendingAddedFunctions("platform.new_tool,platform.other_new", agent.builtin_allowed_tools, groupFunctions, ["platform.new_tool"]),
    ).toEqual([{ groupId: "platform", name: "other_new" }]);
  });

  it("reads the dismissed names from the agent config, strings only", () => {
    expect(dismissedBuiltinFunctions({ builtin_tools_dismissed: ["platform.new_tool", 3] })).toEqual(["platform.new_tool"]);
    expect(dismissedBuiltinFunctions(null)).toEqual([]);
    expect(dismissedBuiltinFunctions({ builtin_tools_dismissed: "x" })).toEqual([]);
  });

  it("adds functions to their group's list, sorted, never to an unscoped group", () => {
    expect(withFunctionsAdded({ platform: ["get_agent"] }, [{ groupId: "platform", name: "new_tool" }, { groupId: "web", name: "web_fetch" }])).toEqual({
      platform: ["get_agent", "new_tool"],
    });
  });
});

describe("NewBuiltinToolsNotice", () => {
  it("renders nothing without a pending function", () => {
    render("/agents/a-1/config/tooling?added=platform.get_agent");
    expect(screen.queryByTestId("new-builtin-tools-notice")).toBeNull();
  });

  it("renders nothing when every named function was dismissed", () => {
    render("/agents/a-1/config/tooling?added=platform.new_tool", true, { builtin_tools_dismissed: ["platform.new_tool"] });
    expect(screen.queryByTestId("new-builtin-tools-notice")).toBeNull();
  });

  it("names one pending function and offers Add and Dismiss to a manager only", () => {
    const { unmount } = render("/agents/a-1/config/tooling?added=platform.new_tool");
    expect(screen.getByText("1 new built-in tool is outside this agent's allowlist")).toBeTruthy();
    expect(screen.getByText("platform.new_tool")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
    expect(screen.queryByTestId("new-builtin-tool-dismiss")).toBeNull();
    unmount();
    render("/agents/a-1/config/tooling?added=platform.new_tool", true);
    expect(screen.getByRole("button", { name: "Add" })).toBeTruthy();
    expect(screen.getByTestId("new-builtin-tool-dismiss").tagName).toBe("BUTTON");
    expect(screen.queryByTestId("new-builtin-tools-add-all")).toBeNull();
    expect(screen.queryByTestId("new-builtin-tools-dismiss-all")).toBeNull();
  });

  it("offers Add all and Dismiss all for two functions, on the title row", () => {
    render("/agents/a-1/config/tooling?added=platform.new_tool,platform.other_new", true);
    expect(screen.getByText("2 new built-in tools are outside this agent's allowlist")).toBeTruthy();
    const addAll = screen.getByTestId("new-builtin-tools-add-all");
    const dismissAll = screen.getByTestId("new-builtin-tools-dismiss-all");
    // On the title row, before the list (UX round 1 B2), not below it.
    const firstRow = screen.getAllByTestId("new-builtin-tool")[0];
    for (const action of [addAll, dismissAll]) {
      expect(action.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(firstRow.parentElement?.contains(action)).toBe(false);
    }
  });

  it("dismisses one function: the server keeps it and the other one remains", async () => {
    render("/agents/a-1/config/tooling?added=platform.new_tool,platform.other_new", true);
    fireEvent.click(screen.getAllByTestId("new-builtin-tool-dismiss")[0]);
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/agents/a-1/builtin-tools/notice", { dismiss: ["platform.new_tool"] }));
    await waitFor(() => expect(screen.getAllByTestId("new-builtin-tool")).toHaveLength(1));
    expect(screen.getByText("platform.other_new")).toBeTruthy();
    expect(api.put).not.toHaveBeenCalled();
  });

  it("dismisses all: the notice goes away and nothing is granted", async () => {
    render("/agents/a-1/config/tooling?added=platform.new_tool,platform.other_new", true);
    fireEvent.click(screen.getByTestId("new-builtin-tools-dismiss-all"));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/agents/a-1/builtin-tools/notice", { dismiss: ["platform.new_tool", "platform.other_new"] }),
    );
    await waitFor(() => expect(screen.queryByTestId("new-builtin-tools-notice")).toBeNull());
    expect(api.put).not.toHaveBeenCalled();
  });

  it("adds a function as before, then asks the server to mark the notice read", async () => {
    render("/agents/a-1/config/tooling?added=platform.new_tool", true);
    fireEvent.click(screen.getByTestId("new-builtin-tool-add"));
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith("/agents/a-1", { builtin_allowed_tools: { platform: ["get_agent", "new_tool"] } }),
    );
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/agents/a-1/builtin-tools/notice", { dismiss: [] }));
  });
});
