/**
 * The command mode (">") of the top-bar search lists commands as one flat list:
 * a parent with `children` (Operations, Tasks) is a menu in the palette, never a
 * row of its own, so its children are listed in its place as
 * "<child category>: <child title>" (UX round 1 on #785, B2). Pure: the
 * catalog is passed in, so the unit test runs it on the real manifests.
 */
import type { CommandDef } from "./types";

/** Every leaf command, children listed in place of their parent; a child without an icon takes its parent's. */
function flattenCommands(commands: CommandDef[]): CommandDef[] {
  const out: CommandDef[] = [];
  for (const command of commands) {
    if (!command.children) {
      out.push(command);
      continue;
    }
    out.push(
      ...flattenCommands(command.children.map((child) => (child.icon || !command.icon ? child : { ...child, icon: command.icon }))),
    );
  }
  return out;
}

/** The leaf commands whose category, title, id or keywords hold `needle` (lower case, trimmed). */
export function matchCommands(commands: CommandDef[], needle: string): CommandDef[] {
  return flattenCommands(commands).filter((command) =>
    `${command.category} ${command.title} ${command.id} ${(command.keywords ?? []).join(" ")}`.toLowerCase().includes(needle),
  );
}
