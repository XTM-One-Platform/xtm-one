/**
 * Command contribution contract — the ⌘K palette's equivalent of VS Code's
 * `contributes.commands`.
 *
 * A screen declares what it can do in a colocated `<Screen>.commands.ts`, which
 * `registry.ts` aggregates at build time. Declarations are **static data**: no
 * imports of the screen, no closures, nothing that would drag a page's chunk
 * into the entry bundle just to list its commands. That is also what lets
 * `scripts/validate-commands.mjs` check the whole catalog by parsing files.
 *
 * Anything that needs live state — the handler that actually runs, or the `@`
 * sections of the page you are on — is registered at runtime by the mounted
 * screen (see `CommandsProvider`).
 */
import type { LucideIcon } from "lucide-react";

/**
 * Context keys a `when` clause may test — the platform/session facts that
 * decide whether a command is offered at all.
 *
 * Keep in sync with the `context` memo in `CommandsProvider` (which supplies
 * the values) and the
 * `CONTEXT_KEYS` copy in `scripts/validate-commands.mjs` (which rejects typos).
 * @public
 */
export const CONTEXT_KEYS = [
  "isAdmin",
  /** The chat surfaces are reachable (read-only in XTM One CE). */
  "chatEnabled",
  /** Messages can actually be sent — the platform is activated. Mirrors the
   *  server's `require_chat_enabled`; use this for anything that starts a
   *  conversation, `chatEnabled` only for navigating to a chat surface. */
  "chatSendEnabled",
  "agenticAI",
  "customVariables",
  "xtmOne",
  "aiAdoption",
  "catalogEnabled",
  /** The `FLOW_BUILDER` feature flag. A `when` key rather than `requires`:
   *  the builder is not a paid add-on to discover, it is a feature that has
   *  not been announced, so its commands must not be listed at all. */
  "flowBuilder",
  /** The `DEEP_INVESTIGATION` feature flag: off, the investigation
   *  packs pages answer 403, so their commands are not listed. */
  "deepInvestigation",
  /** Evaluation: copilot or dev mode with the `EVALUATION` flag on.
   *  Off, its pages redirect home and its API answers 403. */
  "evaluation",
  /** The runner subsystem (`RUNNERS_ENABLED`) - what XTM One Desktop
   *  enrolls into. Off means the Desktop page redirects home, so its command
   *  must not be offered. */
  "runnersEnabled",
  /** The waiting arcade (`ARCADE_ENABLED`). Off means every arcade
   *  route 404s, so the play / leaderboard commands must not be offered. */
  "arcadeEnabled",
  /** On-premises relays (`RELAYS_ENABLED`). Off means the Relays page
   *  explains they are off, so its commands are not offered. */
  "relaysEnabled",
] as const;

type ContextKey = (typeof CONTEXT_KEYS)[number];

export type CommandContext = Record<ContextKey, boolean> & {
  /** Current pathname, for route-scoped commands. */
  route: string;
};

export interface CommandDef {
  /** Dot-namespaced and globally unique, e.g. `"agents.create"`. */
  id: string;
  /** English label — an i18n key, shown after the category. */
  title: string;
  /** Grouping label, VS Code style ("Agents: Create an agent"). An i18n key. */
  category: string;
  /** Extra search terms that should match this command but aren't in its title. */
  keywords?: string[];
  icon?: LucideIcon;
  /** Every listed key must be true for the command to appear. */
  when?: ContextKey[];
  /** No listed key may be true. */
  whenNot?: ContextKey[];
  /**
   * Licence requirement — *not* a visibility rule.
   *
   * `when` hides what does not apply (wrong role, wrong platform mode, feature
   * switched off). A licence gate is different: the feature exists, the user
   * simply has not bought it. Every other surface in the app shows those
   * controls with an EE badge and answers with the upsell dialog, so the
   * palette does the same — hiding them would make the palette the one place
   * where you cannot discover what the add-on unlocks.
   */
  requires?: "agenticAI";
  /** Only offered on these routes (prefix match). Omitted = everywhere. */
  routes?: string[];
  /** Route to open. Most commands are just this. */
  navigate?: string;
  /**
   * Imperative handler id, registered by the screen with `useCommandHandler`.
   * Defaults to the command id. Combined with `navigate`, the palette opens the
   * route first and runs the handler once the screen has registered it — which
   * is how a screen's own actions stay reachable from anywhere.
   */
  handler?: string;
  /** Static sub-entries: selecting the parent pushes this list instead of running. */
  children?: CommandDef[];
}

export interface CommandManifest {
  commands: CommandDef[];
}

/** Declares a screen's commands. One default-exported call per manifest file. */
export function defineCommands(commands: CommandDef[]): CommandManifest {
  return { commands };
}

/** A jump target inside the current screen — the `@` mode. Registered live. */
export interface CommandSection {
  id: string;
  /** English label — an i18n key. */
  title: string;
  /** Runs on select: switch tab, scroll to a block, open a panel. */
  select: () => void;
}
