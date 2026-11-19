/* eslint-disable react-refresh/only-export-components */
/**
 * Runtime half of the command system: what the static manifests cannot hold.
 *
 *  - **Handlers.** A manifest is plain data, so the function a command runs is
 *    registered by the screen while it is mounted (`useCommandHandler`). A
 *    command whose handler is absent but which declares `navigate` is still
 *    offered: the palette opens the route and runs the handler as soon as the
 *    screen registers it (`runCommand` below), so a screen's actions work from
 *    anywhere — VS Code's activation-on-demand, in miniature.
 *  - **Sections.** The `@` mode lists jump targets *inside the current screen*,
 *    which only that screen knows (`useCommandSections`).
 *  - **Context.** The `when` values, assembled once from the platform config.
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useArcade } from "@/components/arcade/arcade-context";
import { useAuth } from "@/hooks/useAuth";
import { useRunnersEnabled } from "@/hooks/useDesktop";
import { useRelaysEnabled } from "@/hooks/api/useRelayApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import {
  useAICatalogEnabled, useAgenticAIEnabled, useChatEnabled, useChatSendEnabled,
  useCustomVariablesEnabled, useIsXtmOne, usePlatformConfig,
} from "@/hooks/usePlatformConfig";
import type { CommandContext, CommandDef, CommandSection } from "./types";

/** How long a pending handler waits for its screen to mount before giving up. */
const PENDING_HANDLER_TIMEOUT_MS = 5_000;

interface CommandsState {
  context: CommandContext;
  sections: CommandSection[];
  registerSections: (sections: CommandSection[]) => () => void;
  registerHandler: (id: string, fn: () => void) => () => void;
  runCommand: (command: CommandDef) => void;
}

const CommandsContext = createContext<CommandsState | null>(null);

export function CommandsProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { isAdmin } = useAuth();
  const platformConfig = usePlatformConfig();
  const chatEnabled = useChatEnabled();
  const chatSendEnabled = useChatSendEnabled();
  const agenticAI = useAgenticAIEnabled();
  const customVariables = useCustomVariablesEnabled();
  const xtmOne = useIsXtmOne();
  const catalogEnabled = useAICatalogEnabled();
  const flowBuilder = useFeatureFlag("FLOW_BUILDER");
  const deepInvestigation = useFeatureFlag("DEEP_INVESTIGATION");
  const evaluation = useFeatureFlag("EVALUATION");
  const runnersEnabled = useRunnersEnabled();
  const arcadeEnabled = useArcade().enabled;
  const relaysEnabled = useRelaysEnabled();

  /* Keyed by registration, not a single array: a screen can render more than
     one tab bar (a page's tabs plus a nested section bar), and both should
     contribute their jump targets rather than the last one winning. */
  const [sectionGroups, setSectionGroups] = useState<Map<number, CommandSection[]>>(new Map());
  const nextGroupId = useRef(0);
  const handlers = useRef(new Map<string, () => void>());
  /** Handler id awaited after a navigation, with its timeout. */
  const pending = useRef<{ id: string; timer: number } | null>(null);

  const context = useMemo<CommandContext>(() => ({
    route: pathname,
    isAdmin,
    chatEnabled,
    chatSendEnabled,
    agenticAI,
    customVariables,
    xtmOne,
    aiAdoption: platformConfig?.features.ai_adoption !== false,
    catalogEnabled,
    flowBuilder,
    deepInvestigation,
    evaluation,
    runnersEnabled: runnersEnabled === true,
    arcadeEnabled,
    relaysEnabled: relaysEnabled === true,
    // Intents are XTM One domain content (#445): Browse intents is not offered in Copilot mode.
    intents: xtmOne,
  }), [pathname, isAdmin, chatEnabled, chatSendEnabled, agenticAI, customVariables,
    xtmOne, platformConfig?.features.ai_adoption, catalogEnabled, flowBuilder, deepInvestigation, evaluation, runnersEnabled,
    arcadeEnabled, relaysEnabled]);

  const clearPending = useCallback(() => {
    if (!pending.current) return;
    window.clearTimeout(pending.current.timer);
    pending.current = null;
  }, []);

  const registerHandler = useCallback((id: string, fn: () => void) => {
    handlers.current.set(id, fn);
    /* A command that navigated here is waiting for exactly this id. */
    if (pending.current?.id === id) {
      clearPending();
      fn();
    }
    return () => {
      if (handlers.current.get(id) === fn) handlers.current.delete(id);
    };
  }, [clearPending]);

  const registerSections = useCallback((next: CommandSection[]) => {
    const id = nextGroupId.current++;
    setSectionGroups((current) => new Map(current).set(id, next));
    return () => setSectionGroups((current) => {
      if (!current.has(id)) return current;
      const without = new Map(current);
      without.delete(id);
      return without;
    });
  }, []);

  const sections = useMemo(() => [...sectionGroups.values()].flat(), [sectionGroups]);

  const runCommand = useCallback((command: CommandDef) => {
    const handlerId = command.handler ?? command.id;
    const handler = handlers.current.get(handlerId);

    if (handler && (!command.navigate || command.navigate === pathname)) {
      handler();
      return;
    }
    if (command.navigate) {
      navigate(command.navigate);
      /* Only wait for a handler the command actually declares — a plain
         navigation command must not arm a pending slot that later steals an
         unrelated registration. */
      if (command.handler) {
        clearPending();
        pending.current = {
          id: handlerId,
          timer: window.setTimeout(() => { pending.current = null; }, PENDING_HANDLER_TIMEOUT_MS),
        };
      }
      return;
    }
    if (handler) handler();
  }, [navigate, pathname, clearPending]);

  /* Never leave a timer behind on unmount. */
  useEffect(() => clearPending, [clearPending]);

  const value = useMemo<CommandsState>(
    () => ({ context, sections, registerSections, registerHandler, runCommand }),
    [context, sections, registerSections, registerHandler, runCommand],
  );

  return <CommandsContext.Provider value={value}>{children}</CommandsContext.Provider>;
}

export function useCommands(): CommandsState {
  const ctx = useContext(CommandsContext);
  if (!ctx) throw new Error("useCommands must be used inside <CommandsProvider>");
  return ctx;
}

/**
 * Binds a manifest command id to what it actually does, for as long as the
 * screen is mounted. `fn` is kept in a ref, so it may close over fresh state
 * without re-registering on every render.
 */
export function useCommandHandler(id: string, fn: () => void) {
  const { registerHandler } = useCommands();
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(
    () => registerHandler(id, () => latest.current()),
    [id, registerHandler],
  );
}

/**
 * Publishes this screen's `@` jump targets.
 *
 * Re-registration is keyed on the section ids, not on array identity: callers
 * routinely build their tab list inline, and depending on the array itself
 * would re-register — and so re-render — on every render. The `select`
 * closures are read from a ref at call time, so they always see fresh state
 * even though the effect does not re-run.
 */
export function useCommandSections(sections: CommandSection[]) {
  const { registerSections } = useCommands();
  const latest = useRef(sections);
  latest.current = sections;
  const signature = sections.map((section) => section.id).join("|");

  useEffect(() => {
    const stable = latest.current.map(({ id, title }) => ({
      id,
      title,
      select: () => latest.current.find((section) => section.id === id)?.select(),
    }));
    return registerSections(stable);
  }, [signature, registerSections]);
}
