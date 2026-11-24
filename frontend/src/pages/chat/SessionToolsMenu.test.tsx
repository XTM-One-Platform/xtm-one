// The composer's Session tools menu: one sub-menu per kind of tool, each row
// a switch that toggles its tool and keeps the menu open, then the actions on
// the whole selection. Before the built-in registry answers, the Built-in
// sub-menu says so and "Enable all" stays disabled instead of enabling
// nothing without a word.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { Globe, ListTodo } from "lucide-react";

import type { BuiltinToolOption } from "@/hooks/useBuiltinTools";
import { renderWithProviders } from "@/testing/providers";
import type { Integration } from "@/types";
import { SessionToolsMenu, type SessionToolsMenuProps } from "./SessionToolsMenu";

vi.mock("@/hooks/api/usePlatformApi", () => ({ usePlatformRegistrations: () => ({ data: [] }) }));

function suite(id: string, label: string, icon = Globe): BuiltinToolOption {
  return {
    id,
    label,
    description: "",
    detail: "",
    tag: "",
    icon,
    available: true,
    agentOnly: false,
    assistantAdminOnly: false,
    internal: false,
    known: true,
    functions: [],
  };
}

const github = { id: "i-github", name: "Github", provider: "github" } as Integration;
const jira = { id: "i-jira", name: "Jira", provider: "jira" } as Integration;

function renderProps(overrides: Partial<SessionToolsMenuProps> = {}): SessionToolsMenuProps {
  return {
    label: "Session tools",
    disabled: false,
    count: 1,
    builtinTools: [suite("web", "Web"), suite("coordination", "Coordination", ListTodo)],
    builtinToolsReady: true,
    builtinToolsFailed: false,
    carriesBuiltin: (ids, suiteId) => ids.includes(suiteId),
    sessionBuiltinIds: ["coordination"],
    onToggleBuiltin: vi.fn(),
    customTools: [],
    sessionCustomToolIds: [],
    onToggleCustomTool: vi.fn(),
    connectedIntegrations: [github, jira],
    chatIntegrations: [github, jira],
    hiddenIntegrationIds: ["i-jira"],
    sessionIntegrationIds: [],
    onToggleIntegration: vi.fn(),
    onHideIntegration: vi.fn(),
    onUnhideIntegration: vi.fn(),
    scopeValues: {},
    onScopeChange: vi.fn(),
    mcpServers: [],
    sessionMCPServerIds: [],
    onToggleMCPServer: vi.fn(),
    knowledgeBases: [],
    sessionKBIds: [],
    onToggleKB: vi.fn(),
    onResetDefault: vi.fn(),
    onEnableAll: vi.fn(),
    onDisableAll: vi.fn(),
    ...overrides,
  };
}

function renderMenu(overrides: Partial<SessionToolsMenuProps> = {}) {
  const props = renderProps(overrides);
  renderWithProviders(<SessionToolsMenu {...props} />);
  fireEvent.pointerDown(screen.getByRole("button", { name: "Session tools" }), { button: 0, ctrlKey: false });
  return props;
}

function openSection(testId: string) {
  fireEvent.click(screen.getByTestId(`${testId}-trigger`));
  return screen.getByTestId(`${testId}-menu`);
}

function renderTrigger(count: number) {
  const props = { ...renderProps(), count };
  return renderWithProviders(<SessionToolsMenu {...props} />);
}

describe("Session tools count badge", () => {
  it("draws a circle for one and two digits and a pill only for the capped 99+", () => {
    for (const [count, text, shape] of [
      [7, "7", "circle"],
      [9, "9", "circle"],
      [10, "10", "circle"],
      [46, "46", "circle"],
      [99, "99", "circle"],
      [100, "99+", "pill"],
      [460, "99+", "pill"],
    ] as const) {
      const { unmount } = renderTrigger(count);
      const badge = screen.getByTestId("session-tools-count");
      expect(badge.textContent).toBe(text);
      expect(badge.dataset.shape).toBe(shape);
      unmount();
    }
  });

  it("draws no badge at 0 and keeps the button's accessible name whatever the count", () => {
    for (const count of [0, 1, 46, 460]) {
      const { unmount } = renderTrigger(count);
      expect(screen.getByRole("button", { name: "Session tools" })).toBeTruthy();
      expect(screen.queryByTestId("session-tools-count") === null).toBe(count === 0);
      unmount();
    }
  });

  it("rounds the badge with rounded-full, never the scoped --radius-lg (8px under .filigran-chatbot)", () => {
    // jsdom computes no Tailwind radius, so the guard reads the source, as the
    // design-system source guards do (LIBRARY-FEEDBACK F47, UX round 1 on #11403).
    const source = readFileSync(join(process.cwd(), "src/pages/chat/SessionToolsMenu.tsx"), "utf8");
    const glyph = source.slice(source.indexOf("function ToolsGlyph"), source.indexOf("</Badge>"));
    expect(glyph).toMatch(/className="[^"]*\brounded-full\b[^"]*"/);
  });
});

describe("SessionToolsMenu", () => {
  it("lists one sub-menu per kind of tool on offer, with the tools on out of the tools listed", () => {
    renderMenu();
    const menu = screen.getByTestId("session-tools-menu");
    expect(within(menu).getAllByRole("menuitem", { expanded: false }).map((row) => row.textContent)).toEqual([
      "Built-in1/2",
      "Integrations0/1",
    ]);
  });

  it("toggles a built-in suite from its switch row and keeps the menu open", () => {
    const props = renderMenu();
    const builtin = openSection("session-tools-builtin");
    const rows = within(builtin).getAllByRole("menuitemcheckbox");
    expect(rows.map((row) => [row.textContent, row.getAttribute("aria-checked")])).toEqual([
      ["Web", "false"],
      ["Coordination", "true"],
    ]);
    fireEvent.click(rows[0]);
    expect(props.onToggleBuiltin).toHaveBeenCalledTimes(1);
    expect(props.onToggleBuiltin).toHaveBeenCalledWith("web");
    expect(screen.getByTestId("session-tools-menu")).not.toBeNull();
  });

  it("says the built-in tools are loading, and keeps Enable all disabled, until the registry answers", () => {
    const props = renderMenu({ builtinTools: [], builtinToolsReady: false });
    const enableAll = screen.getByTestId("session-tools-enable-all");
    expect(enableAll.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(enableAll);
    expect(props.onEnableAll).toHaveBeenCalledTimes(0);
    openSection("session-tools-builtin");
    expect(screen.getByTestId("session-builtin-tools-loading").getAttribute("aria-busy")).toBe("true");
  });

  it("shows an inline error in the Built-in sub-menu when the registry read failed", () => {
    renderMenu({ builtinTools: [], builtinToolsReady: false, builtinToolsFailed: true });
    openSection("session-tools-builtin");
    expect(screen.getByTestId("session-builtin-tools-error").textContent).toContain("Built-in tools could not be loaded");
  });

  it("runs Enable all, Disable all and Reset default once the registry is known", () => {
    const props = renderMenu();
    fireEvent.click(screen.getByTestId("session-tools-enable-all"));
    fireEvent.click(screen.getByTestId("session-tools-disable-all"));
    fireEvent.click(screen.getByTestId("session-tools-reset"));
    expect(props.onEnableAll).toHaveBeenCalledTimes(1);
    expect(props.onDisableAll).toHaveBeenCalledTimes(1);
    expect(props.onResetDefault).toHaveBeenCalledTimes(1);
  });

  it("leaves a hidden integration out of its sub-menu, and shows it again from the visibility dialog", () => {
    const props = renderMenu();
    const integrations = openSection("session-tools-integrations");
    expect(within(integrations).getAllByRole("menuitemcheckbox").map((row) => row.textContent)).toEqual(["Github"]);
    fireEvent.click(within(integrations).getByTestId("session-tools-integrations-visibility"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByTestId("session-tools-integration-visibility-i-jira"));
    fireEvent.click(within(dialog).getByTestId("session-tools-integration-visibility-i-github"));
    expect(props.onUnhideIntegration).toHaveBeenCalledTimes(1);
    expect(props.onUnhideIntegration).toHaveBeenCalledWith("i-jira");
    expect(props.onHideIntegration).toHaveBeenCalledTimes(1);
    expect(props.onHideIntegration).toHaveBeenCalledWith("i-github");
  });

  it("does not open while the composer is disabled", () => {
    renderMenu({ disabled: true });
    expect(screen.queryByTestId("session-tools-menu")).toBeNull();
  });
});
