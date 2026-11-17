/**
 * Build-time aggregation of every screen's command manifest.
 *
 * `import.meta.glob(..., { eager: true })` is resolved by Vite during the build,
 * so this is a static import list — not a runtime directory scan. The catalog
 * therefore contains commands for screens that are **not mounted**, which is the
 * whole point: ⌘K must offer "Agents: Create an agent" from anywhere, exactly as
 * VS Code lists commands from extensions that have not been activated yet.
 *
 * No generated file to keep in sync either; `scripts/validate-commands.mjs`
 * enforces the invariants a codegen step would otherwise have owned.
 */
import type { CommandContext, CommandDef, CommandManifest } from "./types";

const manifests = import.meta.glob<{ default: CommandManifest }>(
  "/src/**/*.commands.ts",
  { eager: true },
);

function collect(): CommandDef[] {
  const out: CommandDef[] = [];
  for (const [path, mod] of Object.entries(manifests)) {
    const manifest = mod.default;
    if (!manifest?.commands) {
      /* A manifest that forgot `export default defineCommands([...])` would
         silently contribute nothing — loud in dev, ignored in production. */
      if (import.meta.env.DEV) console.error(`[commands] ${path} has no default export`);
      continue;
    }
    out.push(...manifest.commands);
  }
  return out;
}

const ALL_COMMANDS: CommandDef[] = collect();

/* Duplicate ids make `useCommandHandler` ambiguous — the validation script is
   the real gate, but a dev-time shout catches it before the commit. */
if (import.meta.env.DEV) {
  const seen = new Set<string>();
  const walk = (commands: CommandDef[]) => {
    for (const command of commands) {
      if (seen.has(command.id)) console.error(`[commands] duplicate id: ${command.id}`);
      seen.add(command.id);
      if (command.children) walk(command.children);
    }
  };
  walk(ALL_COMMANDS);
}

/** Does `pathname` sit on (or under) `route`? */
function routeMatches(route: string, pathname: string): boolean {
  return route === "/" ? pathname === "/" : pathname === route || pathname.startsWith(`${route}/`);
}

function isAvailable(command: CommandDef, ctx: CommandContext): boolean {
  if (command.when?.some((key) => !ctx[key])) return false;
  if (command.whenNot?.some((key) => ctx[key])) return false;
  if (command.routes && !command.routes.some((route) => routeMatches(route, ctx.route))) return false;
  return true;
}

/**
 * The commands offered right now, children filtered too. A parent whose
 * children all fail their `when` is dropped rather than shown as a dead end.
 */
export function resolveCommands(ctx: CommandContext, commands = ALL_COMMANDS): CommandDef[] {
  const out: CommandDef[] = [];
  for (const command of commands) {
    if (!isAvailable(command, ctx)) continue;
    if (!command.children) {
      out.push(command);
      continue;
    }
    const children = resolveCommands(ctx, command.children);
    if (children.length > 0) out.push({ ...command, children });
  }
  return out;
}
