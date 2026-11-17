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

import { useMemo, useState } from "react";
import { Check, Copy, Download, Ellipsis, KeyRound, Server, TriangleAlert } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import {
  downloadXtmMcpPackage,
  fetchXtmMcpSnippet,
  type XtmMcpPackageFormat,
  type XtmMcpSnippet,
} from "@/hooks/api/usePlatformApi";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { usePlatformMCPServerList } from "@/components/ui/platform-mcp-server-list";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { downloadErrorDescription } from "@/lib/download-error";

/**
 * Installer downloads for the platform tools MCP server (`/mcp/xtm`).
 *
 * Shared by Profile > MCP Endpoint and Settings > Integrations > MCP Endpoint so both offer
 * the same packages with the same warnings — an admin checking what users get
 * should be looking at the same thing they get.
 *
 * The one-click switch is what makes a download a credential: the server mints
 * a dedicated API key and writes it into the package. It is on by default
 * because that is the flow people want, and the panel underneath states the
 * consequence plainly rather than hiding it in the archive's README.
 *
 * The Agent Plugins package never carries a key — the specification requires
 * `mcp.json` headers to be literal, non-secret package data — so its row says
 * so instead of quietly ignoring the switch.
 */

interface PackageOption {
  format: XtmMcpPackageFormat;
  label: string;
  hint: string;
  /** Whether the format can carry an embedded credential at all. */
  canEmbedKey: boolean;
}

interface XtmMcpPackageDownloadProps {
  /** Number of API keys the user already has; drives the no-key notice. */
  apiKeyCount?: number;
  /** Dim every control (Settings renders this when the toggle is off). */
  disabled?: boolean;
  /**
   * Tighter layout for a page where this is a secondary affordance rather
   * than the subject: buttons on one row, no per-format hints, and the
   * credential warning reduced to a single line. The warning is *shortened*,
   * never dropped — a one-click download is a live secret wherever it is
   * offered, and a compact surface is not a reason to stop saying so.
   */
  compact?: boolean;
}

export function XtmMcpPackageDownload({
  apiKeyCount,
  disabled = false,
  compact = false,
}: XtmMcpPackageDownloadProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const platformName = usePlatformName();
  const [includeKey, setIncludeKey] = useState(true);
  const [pending, setPending] = useState<XtmMcpPackageFormat | null>(null);
  // Which format the target picker is choosing for; null means it is closed.
  const [picking, setPicking] = useState<XtmMcpPackageFormat | null>(null);
  // "Other clients": Codex/Cursor install by editing a file, so they get a
  // fragment to merge rather than a package to download.
  const [otherOpen, setOtherOpen] = useState(false);
  const [otherTarget, setOtherTarget] = useState("xtm");
  const [snippet, setSnippet] = useState<XtmMcpSnippet | null>(null);
  const [snippetLoading, setSnippetLoading] = useState<string | null>(null);
  const [snippetCopied, setSnippetCopied] = useState(false);

  const {
    enabled: platformMcpEnabled,
    servers,
    xtmServer,
  } = usePlatformMCPServerList();

  /**
   * One package installs one server, so a download needs a target.
   *
   * Only live servers are offered: an unregistered bridge answers 404, and a
   * package for it would be an installer for nothing. There is deliberately
   * no "all of them" entry — a client loads every tool of every server it
   * connects to, and the four together are far past the point where tool
   * selection degrades. Installing two packages stays the user's call.
   */
  const targets = useMemo(() => {
    const out: { key: string; label: string; provider?: string; toolCount?: number }[] = [];
    if (xtmServer?.enabled) {
      out.push({ key: "xtm", label: platformName || "XTM One" });
    }
    if (platformMcpEnabled) {
      for (const s of servers) {
        if (s.connected) {
          out.push({ key: s.provider, label: s.name, provider: s.provider, toolCount: s.tool_count });
        }
      }
    }
    return out;
  }, [xtmServer, platformMcpEnabled, servers, platformName]);

  const options: PackageOption[] = [
    {
      format: "claude-code",
      label: t_i18n("Claude Code"),
      hint: t_i18n("Plugin with the skill, installed from the terminal"),
      canEmbedKey: true,
    },
    {
      format: "mcpb",
      label: t_i18n("Claude Desktop"),
      hint: t_i18n("Extension installed by double-clicking the file"),
      canEmbedKey: true,
    },
    {
      format: "agent-plugin",
      label: t_i18n("Agent Plugins"),
      hint: t_i18n("Vendor-neutral package for any compatible client"),
      canEmbedKey: false,
    },
  ];

  /** Ask only when there is something to ask: one live server needs no picker. */
  const startDownload = (option: PackageOption) => {
    if (targets.length > 1) {
      setPicking(option.format);
      return;
    }
    void handleDownload(option, targets[0]?.key ?? "xtm");
  };

  const openOther = () => {
    setSnippet(null);
    setOtherTarget(targets[0]?.key ?? "xtm");
    setOtherOpen(true);
  };

  const loadSnippet = async (client: string) => {
    setSnippetLoading(client);
    try {
      const result = await fetchXtmMcpSnippet(client, includeKey, otherTarget);
      setSnippet(result);
      if (result.api_key_created) {
        toast({
          title: t_i18n("API key \"{name}\" created", { name: result.api_key_created }),
          description: t_i18n("It is inside the snippet below. Revoke it from the API Keys tab if it leaks."),
          variant: "success",
        });
      }
    } catch (err) {
      toast({
        title: t_i18n("Could not build the snippet"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setSnippetLoading(null);
    }
  };

  const handleDownload = async (option: PackageOption, target: string) => {
    setPicking(null);
    setPending(option.format);
    try {
      const { keyCreated } = await downloadXtmMcpPackage(
        option.format,
        includeKey && option.canEmbedKey,
        target,
      );
      toast({
        title: keyCreated
          ? t_i18n("Package downloaded: API key \"{name}\" created", { name: keyCreated })
          : t_i18n("Package downloaded"),
        description: keyCreated
          ? t_i18n("The key is inside the file. Delete the file once installed, and revoke the key from the API Keys tab if it leaks.")
          : undefined,
        variant: "success",
      });
    } catch (err) {
      toast({
        title: t_i18n("Download failed"),
        // The package's name comes with the response, so a failure has none.
        description: downloadErrorDescription(err, null, t_i18n),
        variant: "error",
      });
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="space-y-3">
      {/* A plain yes/no on a named thing: a switch labelled "one-click
          install" left the user unable to tell that what it toggled was a
          credential inside the archive. The hint beside it carries the part
          the label cannot — the key is *minted for this download*, not an
          existing one, because stored keys are hashes and cannot be re-read. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <p className="content-compact font-medium text-default-secondary">
          {t_i18n("Include the API key")}
        </p>
        <SegmentedFilter
          options={[
            { key: "key", label: "Yes" },
            { key: "none", label: "No" },
          ]}
          value={includeKey ? "key" : "none"}
          onChange={(k) => !disabled && setIncludeKey(k === "key")}
          ariaLabel={t_i18n("Include the API key")}
          size="md"
        />
        <span className="content-compact text-default-secondary">
          {includeKey
            ? t_i18n("a new key is created and embedded: nothing is asked at install")
            : t_i18n("your client asks for one at install")}
        </span>
      </div>

      {compact ? (
        <div className="flex flex-wrap items-center justify-center gap-2">
          {options.map((option) => (
            <Tip
              key={option.format}
              content={
                includeKey && !option.canEmbedKey
                  ? t_i18n("No key: the standard forbids credentials in the package.")
                  : option.hint
              }
            >
              <Button
                type="button"
                variant="outline"
                className="gap-1.5 content-compact"
                disabled={disabled || pending !== null}
                aria-busy={pending === option.format || undefined}
                onClick={() => startDownload(option)}
              >
                {pending === option.format && <Spinner size="sm" tone="inherit" />}
                {pending !== option.format && <Download className="h-3 w-3" />}
                {option.label}
              </Button>
            </Tip>
          ))}
          <Tip content={t_i18n("Codex, ChatGPT desktop, Cursor, VS Code")}>
            <Button
              type="button"
              variant="outline"
              className="gap-1.5 content-compact"
              disabled={disabled}
              onClick={openOther}
            >
              <Ellipsis className="h-3 w-3" />
              {t_i18n("Other")}
            </Button>
          </Tip>
        </div>
      ) : (
      <div className="grid gap-2 sm:grid-cols-3">
        {options.map((option) => (
          <div
            key={option.format}
            className="rounded-sm bg-elevation-default p-3 space-y-2"
          >
            <div>
              <p className="content-compact font-medium">{option.label}</p>
              <p className="content-compact text-default-secondary">{option.hint}</p>
            </div>
            <Button
              type="button"
              variant="outline"
              className="w-full gap-1.5 content-compact"
              disabled={disabled || pending !== null}
              aria-busy={pending === option.format || undefined}
              onClick={() => startDownload(option)}
            >
              {pending === option.format && <Spinner size="sm" tone="inherit" />}
              {pending !== option.format && <Download className="h-3 w-3" />}
              {t_i18n("Download")}
            </Button>
            {includeKey && !option.canEmbedKey && (
              <p className="content-compact text-default-secondary">
                {t_i18n("No key: the standard forbids credentials in the package.")}
              </p>
            )}
          </div>
        ))}
        <div className="rounded-sm bg-elevation-default p-3 space-y-2">
          <div>
            <p className="content-compact font-medium">{t_i18n("Other")}</p>
            <p className="content-compact text-default-secondary">
              {t_i18n("Codex, ChatGPT desktop, Cursor, VS Code")}
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            className="w-full gap-1.5 content-compact"
            disabled={disabled}
            onClick={openOther}
          >
            <Ellipsis className="h-3 w-3" />
            {t_i18n("Show config")}
          </Button>
        </div>
      </div>
      )}

      {/* Only the two things the control above does NOT already say: the
          risk a credential in the file creates, and the missing prerequisite
          when there is none. The neutral "no credential here" case needs no
          panel — the segmented control states it. */}
      {includeKey ? (
        compact ? (
          <p className="content-compact text-feedback-warning-primary">
            {t_i18n("The downloaded file will hold a live key: don't share it, and delete it once installed.")}
          </p>
        ) : (
          <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2">
            <TriangleAlert className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
            <p className="content-compact text-feedback-warning-primary">
              {t_i18n("The downloaded file will hold a live key: do not share it, and delete it once installed. Revoke that key from the API Keys tab to cut the client off. It is a key of its own, so nothing else breaks.")}
            </p>
          </div>
        )
      ) : apiKeyCount === 0 ? (
        compact ? (
          <p className="content-compact text-feedback-warning-primary">
            {t_i18n("You have no API key yet: create one in the API Keys tab.")}
          </p>
        ) : (
          <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2">
            <KeyRound className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
            <p className="content-compact text-feedback-warning-primary">
              {t_i18n("You have no API key yet: create one in the API Keys tab, or answer Yes above to have a dedicated key created for you.")}
            </p>
          </div>
        )
      ) : null}

      <Dialog open={otherOpen} onOpenChange={setOtherOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t_i18n("Other clients")}</DialogTitle>
            <DialogDescription>
              {t_i18n("These clients are configured by editing a file that already holds your other MCP servers, so this is a fragment to merge: not a file to replace.")}
            </DialogDescription>
          </DialogHeader>

          {targets.length > 1 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="content-compact text-default-secondary">{t_i18n("Server")}</span>
              <SegmentedFilter
                options={targets.map((tg) => ({ key: tg.key, label: tg.label }))}
                value={otherTarget}
                onChange={(k) => { setOtherTarget(k); setSnippet(null); }}
                ariaLabel={t_i18n("Server")}
              />
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            {[
              { key: "codex", label: t_i18n("Codex / ChatGPT desktop") },
              { key: "cursor", label: "Cursor" },
            ].map((c) => (
              <Button
                key={c.key}
                type="button"
                variant={snippet?.client === c.key ? "default" : "outline"}
                className="gap-1.5 content-compact"
                disabled={disabled || snippetLoading !== null}
                aria-busy={snippetLoading === c.key || undefined}
                onClick={() => void loadSnippet(c.key)}
              >
                {snippetLoading === c.key && <Spinner size="sm" tone="inherit" />}
                {c.label}
              </Button>
            ))}
          </div>

          {snippet && (
            <div className="min-w-0 space-y-2">
              <div className="flex min-w-0 items-center gap-2">
                <span className="shrink-0 content-compact text-default-secondary">
                  {t_i18n("Merge into")}
                </span>
                <code className="min-w-0 truncate rounded-sm bg-elevation-hover px-1.5 py-0.5 content-compact font-mono-plex">
                  {snippet.path}
                </code>
                <Button
                  type="button"
                  variant="ghost"
                  className="ml-auto shrink-0 gap-1.5 content-compact"
                  onClick={() => {
                    navigator.clipboard.writeText(snippet.content);
                    setSnippetCopied(true);
                    setTimeout(() => setSnippetCopied(false), COPY_FEEDBACK_DELAY);
                  }}
                >
                  {snippetCopied ? (
                    <Check className="h-3 w-3 text-feedback-success-primary" />
                  ) : (
                    <Copy className="h-3 w-3" />
                  )}
                  {t_i18n("Copy")}
                </Button>
              </div>
              <pre className="w-full max-h-56 overflow-auto rounded-sm bg-elevation-hover p-3 content-compact font-mono-plex whitespace-pre">
                {snippet.content}
              </pre>
              {!includeKey && (
                <p className="content-compact text-default-secondary">
                  {t_i18n("Export XTM_ONE_API_KEY in your environment with one of your API keys before starting the client.")}
                </p>
              )}
            </div>
          )}

          <p className="content-compact text-default-secondary border-t border-elevation-subtle-soft pt-3">
            {t_i18n("VS Code needs no snippet: it reads the Agent Plugins package directly. Download that one instead.")}
          </p>
        </DialogContent>
      </Dialog>

      <Dialog open={picking !== null} onOpenChange={(o) => !o && setPicking(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t_i18n("Which server?")}</DialogTitle>
            <DialogDescription>
              {t_i18n("A package installs one MCP server. Pick the one you want: you can install another afterwards.")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            {targets.map((target) => (
              <button
                key={target.key}
                type="button"
                className="flex w-full items-center gap-3 rounded-sm bg-elevation-default px-3 py-2.5 text-left transition-colors hover:bg-elevation-hover"
                onClick={() => {
                  const option = options.find((o) => o.format === picking);
                  if (option) void handleDownload(option, target.key);
                }}
              >
                {target.provider ? (
                  <ProviderLogo provider={target.provider} className="h-5 w-5 shrink-0" />
                ) : (
                  <Server className="h-5 w-5 shrink-0 text-accent" />
                )}
                <span className="min-w-0 flex-1 truncate content-base">{target.label}</span>
                {target.toolCount !== undefined && (
                  <span className="shrink-0 content-compact text-default-secondary tabular-nums">
                    {t_i18n("{count} tools", { count: target.toolCount })}
                  </span>
                )}
              </button>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
