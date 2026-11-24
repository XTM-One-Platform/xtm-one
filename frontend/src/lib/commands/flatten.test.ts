/**
 * Ctrl K command mode lists the children of a parent command (UX round 1 on #785, B2):
 * every navigate child of every *.commands.ts manifest is returned for ">" + its title,
 * labelled by its own category ("Tasks: Value"). `yarn test`.
 */
import { describe, expect, it } from "vitest";
import { matchCommands } from "./flatten";
import type { CommandDef, CommandManifest } from "./types";

const manifests = import.meta.glob<{ default: CommandManifest }>("/src/**/*.commands.ts", { eager: true });
const ALL: CommandDef[] = Object.values(manifests).flatMap((mod) => mod.default.commands);

function navigateChildren(commands: CommandDef[]): CommandDef[] {
  return commands.flatMap((command) =>
    command.children ? [...command.children.filter((child) => child.navigate), ...navigateChildren(command.children)] : [],
  );
}

describe("command mode flattening", () => {
  it("returns every navigate child of every manifest for its title", () => {
    const children = navigateChildren(ALL);
    expect(children.length).toBeGreaterThan(0);
    for (const child of children) {
      const ids = matchCommands(ALL, child.title.toLowerCase()).map((command) => command.id);
      expect(ids, `">${child.title}" does not list ${child.id}`).toContain(child.id);
    }
  });

  it("lists the Tasks and Operations tabs and never a parent row", () => {
    const flat = matchCommands(ALL, "");
    expect(flat.some((command) => command.children)).toBe(false);
    const tasks = matchCommands(ALL, "tasks").filter((command) => command.category === "Tasks").map((command) => command.navigate);
    expect(tasks).toEqual(expect.arrayContaining(["/tasks/work-items", "/tasks/value", "/tasks/workspaces"]));
    const operations = matchCommands(ALL, "operations").filter((command) => command.category === "Operations").map((command) => command.navigate);
    expect(operations).toEqual(
      expect.arrayContaining(["/operations/executions", "/operations/background-tasks", "/operations/shared-state", "/operations/worksets"]),
    );
    expect(matchCommands(ALL, "worksets").map((command) => command.navigate)).toContain("/operations/worksets");
  });

  it("gives a child without an icon its parent's icon", () => {
    const parent = ALL.find((command) => command.id === "tasks.open");
    expect(parent?.icon).toBeTruthy();
    const value = matchCommands(ALL, "").find((command) => command.id === "tasks.value");
    expect(value?.icon).toBe(parent?.icon);
  });
});
