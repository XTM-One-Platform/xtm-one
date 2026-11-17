// The shared tool scope dialog names what it lists: an integration's
// "tools", a built-in group's "functions", with no em dash in its title, and its
// parent-agent note and its none-selected warning drawn by the dialog itself (their
// colour is checked at 4.5:1 in tests_unit/warning-text-contrast.test.ts).
// Rows: a row given a label shows it as the primary text with the
// function id beside it; a row without a label keeps its name.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { ToolScopeDialog } from "./tool-scope-dialog";

const TOOLS = [
  { name: "list_work_items", description: "" },
  { name: "get_work_item", description: "" },
  { name: "create_work_item", description: "" },
];

function open(props: Partial<React.ComponentProps<typeof ToolScopeDialog>> = {}) {
  return renderWithProviders(
    <ToolScopeDialog
      tools={TOOLS}
      isLoading={false}
      currentAllowed={["list_work_items", "get_work_item"]}
      sourceLabel="Work Items"
      sourceType="built-in tool"
      contextLabel="assignment"
      onSave={() => {}}
      onClose={() => {}}
      {...props}
    />,
  );
}

describe("ToolScopeDialog unit noun", () => {
  it("speaks of functions for a built-in group", () => {
    open({ unit: "function" });
    const title = screen.getByRole("heading", { name: /Function Scope: Work Items/ });
    expect(title.textContent).not.toContain("\u2014");
    expect(screen.getByRole("button", { name: "All functions" })).toBeInTheDocument();
    expect(screen.getByText("2 of 3 functions selected")).toBeInTheDocument();
    expect(screen.getByText(/Choose which functions from this built-in tool/)).toBeInTheDocument();
    expect(screen.queryByText(/tools/i)).toBeNull();
  });

  it("keeps speaking of tools by default, without the em dash", () => {
    open({ sourceLabel: "GitHub", sourceType: "integration" });
    const title = screen.getByRole("heading", { name: /Tool Scope: GitHub/ });
    expect(title.textContent).not.toContain("\u2014");
    expect(screen.getByRole("button", { name: "All tools" })).toBeInTheDocument();
    expect(screen.getByText("2 of 3 selected")).toBeInTheDocument();
  });

  it("draws the parent-agent note it is given", () => {
    open({ unit: "function", note: "Note: Only showing 3 functions allowed by the parent agent's scoping." });
    expect(screen.getByTestId("tool-scope-note")).toHaveTextContent(
      "Note: Only showing 3 functions allowed by the parent agent's scoping.",
    );
  });

  it("warns without an em dash when no tool is selected", () => {
    open({ sourceLabel: "GitHub", sourceType: "integration", currentAllowed: [] });
    const warning = screen.getByTestId("tool-scope-none-warning");
    expect(warning).toHaveTextContent("No tools selected: the assignment will not be able to use this integration.");
    expect(warning.textContent).not.toContain("\u2014");
  });

  it("warns in functions when no function is selected", () => {
    open({ unit: "function", currentAllowed: [] });
    expect(screen.getByTestId("tool-scope-none-warning")).toHaveTextContent(
      "No functions selected: the assignment will not be able to use this built-in tool.",
    );
  });
});

describe("ToolScopeDialog rows", () => {
  it("shows the label of a row as primary text and its id as secondary text", () => {
    renderWithProviders(
      <ToolScopeDialog
        tools={[{ name: "inspect_my_traces", label: "Inspect my traces", description: "" }]}
        isLoading={false}
        currentAllowed={null}
        sourceLabel="Traces"
        sourceType="built-in tool"
        contextLabel="agent"
        onSave={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    const row = screen.getByRole("button", { name: /Inspect my traces/ });
    expect(row.textContent).toContain("inspect_my_traces");
  });

  it("shows the name of a row without a label", () => {
    renderWithProviders(
      <ToolScopeDialog
        tools={[{ name: "search_notion", description: "" }]}
        isLoading={false}
        currentAllowed={null}
        sourceLabel="Notion"
        sourceType="integration"
        contextLabel="agent"
        onSave={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /search_notion/ })).toBeTruthy();
  });
});
