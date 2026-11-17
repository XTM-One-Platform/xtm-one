/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Local paths in the chat, made actionable inside XTM One Desktop.
 *
 * A path the agent touched on the machine the app runs on - the file it read
 * or wrote, the folder it listed, a path quoted in its reply - gets `Open`
 * (the default application, passive content only), `Show in folder` (the OS
 * file manager, the item selected) and `Copy path`. The native shell executes
 * and re-validates every request (`local_paths.rs`); this component only
 * decides what is worth offering, from the bridge's announced capabilities.
 * Outside the app, or for another machine's path, only `Copy path` remains.
 */
import { useCallback, useState, type ReactNode } from "react";
import { ExternalLink, File, Folder, FolderOpen } from "lucide-react";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import {
  canActOnLocalPath,
  getDesktopBridge,
  localPathFailure,
  openLocalPath,
  revealLocalPath,
  type LocalPathFailure,
} from "@/lib/desktop";
import { canOpenLocalPath, localPathExtension } from "@/lib/local-paths";
import { cn } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

type LocalPathAction = "open" | "reveal";

/** What a refusal tells the person (`unfocused` / `too_fast` stay silent: a double click, a window switch). */
const FAILURE_MESSAGES: Record<Exclude<LocalPathFailure, "unfocused" | "too_fast">, string> = {
  invalid_path: "This is not a path on this machine.",
  remote_path: "Network locations cannot be opened from the chat. Copy the path instead.",
  not_found: "This item does not exist on this machine (any more).",
  not_openable: "This kind of item is not opened from the chat. Use Show in folder instead.",
  failed: "The item could not be opened.",
};

/**
 * Run a local-path action and say why when the shell refuses it. The shell's
 * `open` shows in its folder whatever it will not open (an extension-less
 * file, an application bundle) - the person asked to SEE it - in the same
 * request: a second one would meet its rate gate.
 */
function useLocalPathAction() {
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const [busy, setBusy] = useState<LocalPathAction | null>(null);

  const run = useCallback(
    async (action: LocalPathAction, path: string) => {
      setBusy(action);
      try {
        await (action === "open" ? openLocalPath(path) : revealLocalPath(path));
      } catch (error) {
        const failure = localPathFailure(error);
        if (failure === "unfocused" || failure === "too_fast") return;
        toast({ title: t_i18n(FAILURE_MESSAGES[failure]), description: path, variant: "error" });
      } finally {
        setBusy(null);
      }
    },
    [toast, t_i18n],
  );

  return { busy, run };
}

const ACTION_BUTTON =
  "rounded-sm p-1 text-default-secondary transition-colors hover:bg-elevation-hover hover:text-accent focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus disabled:opacity-50";

/**
 * The action buttons for one path. `runnerId` is the machine the path belongs
 * to (a card's runner); `undefined` for a path quoted in a reply, which names
 * the machine the app runs on.
 */
export function LocalPathActions({
  path,
  isDir = false,
  runnerId,
  className,
}: {
  path: string;
  isDir?: boolean;
  runnerId?: string | null;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { busy, run } = useLocalPathAction();
  const actionable = canActOnLocalPath(runnerId);
  const openable = actionable && canOpenLocalPath(path, isDir, getDesktopBridge()?.open_extensions ?? []);

  return (
    <span className={cn("inline-flex shrink-0 items-center gap-0.5", className)}>
      {openable && (
        <Tip content={isDir ? t_i18n("Open folder") : t_i18n("Open with the default app")}>
          <button
            type="button"
            aria-label={isDir ? t_i18n("Open folder") : t_i18n("Open with the default app")}
            disabled={busy !== null}
            onClick={() => void run("open", path)}
            className={ACTION_BUTTON}
          >
            {busy === "open" && <Spinner size="sm" tone="inherit" />}{!(busy === "open") && <ExternalLink className="h-3.5 w-3.5" />}
          </button>
        </Tip>
      )}
      {actionable && (
        <Tip content={t_i18n("Show in folder")}>
          <button
            type="button"
            aria-label={t_i18n("Show in folder")}
            disabled={busy !== null}
            onClick={() => void run("reveal", path)}
            className={ACTION_BUTTON}
          >
            {busy === "reveal" && <Spinner size="sm" tone="inherit" />}{!(busy === "reveal") && <FolderOpen className="h-3.5 w-3.5" />}
          </button>
        </Tip>
      )}
      <CopyIconButton value={path} label={t_i18n("Copy path")} />
    </span>
  );
}

/** A path line of a card: the icon, the path (monospace, middle of a long one elided by the browser), the actions. */
export function LocalPathLine({
  path,
  isDir = false,
  runnerId,
  className,
}: {
  path: string;
  isDir?: boolean;
  runnerId?: string | null;
  className?: string;
}) {
  const Icon = isDir ? Folder : File;
  return (
    <div className={cn("flex min-w-0 items-center gap-2", className)}>
      <Icon className={cn("h-3.5 w-3.5 shrink-0", isDir ? "text-feedback-warning-primary" : "text-accent/70")} aria-hidden />
      <Tip content={path}>
        <span className="min-w-0 truncate font-mono-plex content-compact text-default-secondary">{path}</span>
      </Tip>
      <LocalPathActions path={path} isDir={isDir} runnerId={runnerId} className="ml-auto" />
    </div>
  );
}

/**
 * An absolute local path quoted in a reply (inline code or a `file:` link),
 * inside XTM One Desktop: a click opens it (or shows it, for what the shell
 * does not open), Alt / Option + click always shows it in its folder. One
 * button, so the chip takes no more room in the sentence than the code did.
 */
export function LocalPathInline({ path, children }: { path: string; children?: ReactNode }) {
  const { t_i18n } = useFormatter();
  const { busy, run } = useLocalPathAction();
  const bridge = getDesktopBridge();
  // No extension: maybe a folder - the shell opens a folder and shows a file.
  const primary: LocalPathAction =
    localPathExtension(path) === null || canOpenLocalPath(path, false, bridge?.open_extensions ?? [])
      ? "open"
      : "reveal";
  const tip =
    primary === "open"
      ? t_i18n("Open on this machine - {modifier}+click shows it in its folder", {
          modifier: bridge?.platform === "macos" ? "Option" : "Alt",
        })
      : t_i18n("Show in folder");

  return (
    <Tip content={tip}>
      <button
        type="button"
        onClick={(event) => void run(event.altKey ? "reveal" : primary, path)}
        disabled={busy !== null}
        className={cn(
          "inline-flex max-w-full items-center gap-1 rounded-sm bg-elevation-hover px-1.5 py-0.5 text-left align-baseline font-mono-plex text-[13px] text-accent/90",
          "underline decoration-accent/30 decoration-dotted underline-offset-2 transition-colors",
          "hover:bg-accent/10 hover:decoration-accent/70 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
        )}
      >
        {busy !== null ? (
          <Spinner size="sm" tone="inherit" className="shrink-0" label={t_i18n("Loading...")} />
        ) : primary === "open" ? (
          <File className="h-3 w-3 shrink-0 opacity-70" aria-hidden />
        ) : (
          <FolderOpen className="h-3 w-3 shrink-0 opacity-70" aria-hidden />
        )}
        <span className="break-all">{children ?? path}</span>
      </button>
    </Tip>
  );
}
