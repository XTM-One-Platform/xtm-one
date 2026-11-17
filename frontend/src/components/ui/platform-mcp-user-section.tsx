// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Skeleton } from "@/components/ui/skeleton";
import { Tip } from "@/components/ui/tooltip";
import { usePlatformMCPServerList } from "@/components/ui/platform-mcp-server-list";
import { XtmMcpPackageDownload } from "@/components/ui/xtm-mcp-package-download";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useApiKeys } from "@/hooks/api/useMiscApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * The servers this platform **hosts**, as a status strip.
 *
 * The mirror image of the page it sits on: below, MCP servers are things the
 * user configures so agents can call *out*; here they are endpoints the
 * platform exposes so external clients can call *in*. Same protocol, opposite
 * direction — which is why they are separate zones rather than one list.
 *
 * Deliberately tiny. A user cannot change any of this (the toggles are
 * admin-only, in Settings > Integrations > MCP Endpoint), so the only things worth pixels are
 * *which* endpoints exist and *what state* each one is in. Endpoint URLs, tool
 * counts and product blurbs all live one hover away instead of taking a card
 * each — the detailed rendering still exists for Profile and Settings, where
 * connecting a client is the actual task.
 *
 * Every endpoint *the current mode exposes* is listed at all times, including
 * the ones an administrator has switched off. Hiding those made the strip
 * answer a different question than the one a user asks of it: "what can this
 * platform expose?" — and a disabled endpoint that is simply absent is
 * indistinguishable from one that does not exist, which sends people to
 * support instead of to their admin. A dimmed row reading "Disabled" says
 * which it is.
 *
 * The qualifier is load-bearing and was missing until review caught it. In
 * Copilot mode the shared hook drops bridges that were never registered
 * (``isXtmOne ? [...connected, ...notConnected] : connected``), so the
 * ``pending`` state below is XTM One only and that mode shows live /
 * unreachable / disabled. Not forked to an unfiltered catalogue just to make
 * this paragraph true everywhere: the hook's filtering is a deliberate
 * product decision — three permanently grey rows would be noise on a surface
 * whose whole point is to be tiny — and it is shared with Profile, Settings
 * and the package download.
 */

/**
 * Four states, and the dot is the only thing carrying the difference at a
 * glance — so "not connected" gets its own colour rather than sharing grey
 * with "disabled". They are not the same situation: one needs an
 * administrator to flip a switch, the other needs a platform to finish
 * registering, and amber says "waiting on something" where grey says "off".
 */
type DotTone = "live" | "pending" | "error" | "disabled";

const DOT_CLASS: Record<DotTone, string> = {
  live: "bg-feedback-success-primary",
  pending: "bg-feedback-alert-primary",
  error: "bg-feedback-error-primary",
  disabled: "bg-[var(--text-default-secondary)]",
};

interface StripItem {
  key: string;
  name: string;
  provider?: string;
  tone: DotTone;
  status: string;
  detail?: string;
}

export function PlatformMcpUserSection() {
  const { t_i18n } = useFormatter();
  const platformName = usePlatformName();
  // Drives the "you have no API key yet" hint: without it a user who has
  // none is told their client will ask for one, and not where to get it.
  const { data: apiKeys } = useApiKeys();
  const {
    enabled: platformMcpEnabled,
    servers,
    isLoading,
    isXtmOne,
    xtmServer,
  } = usePlatformMCPServerList();

  // The admin toggles decide the *state* of a row now, not whether it exists.
  const xtmEnabled = Boolean(xtmServer?.enabled);
  // Only the package download still depends on the XTM server actually being
  // on: there is nothing to install for an endpoint that refuses connections.
  const showXtm = xtmEnabled;
  const showBridges = isXtmOne || servers.length > 0;

  const items = useMemo<StripItem[]>(() => {
    const out: StripItem[] = [];
    if (xtmServer) {
      out.push({
        key: "xtm",
        // The platform's own server carries the platform's own name — the
        // brand, so it reads as "XTM One" or "Copilot" per deployment mode,
        // not as a generic label next to three named products.
        name: platformName || "XTM One",
        tone: xtmEnabled ? "live" : "disabled",
        status: xtmEnabled ? t_i18n("Enabled") : t_i18n("Disabled"),
        // A URL nobody can call is not a detail worth showing.
        detail: xtmEnabled ? xtmServer.url : undefined,
      });
    }
    if (showBridges) {
      for (const s of servers) {
        // The global toggle outranks the registration: a bridge that is
        // connected but switched off platform-wide still refuses every call,
        // so reporting it as "Connected" would be a lie a user acts on.
        const tone: DotTone = !platformMcpEnabled
          ? "disabled"
          : !s.connected
            ? "pending"
            : s.healthy
              ? "live"
              : "error";
        out.push({
          key: s.provider,
          name: s.name,
          provider: s.provider,
          tone,
          status: !platformMcpEnabled
            ? t_i18n("Disabled")
            : !s.connected
              ? t_i18n("Not connected")
              // Registered but stale heartbeat is a fault, not an absence: the
              // platform believes it is connected and calls will fail.
              : s.healthy
                ? t_i18n("Connected")
                : t_i18n("Unreachable"),
          detail: platformMcpEnabled && s.connected ? s.url : undefined,
        });
      }
    }
    return out;
  }, [xtmServer, xtmEnabled, showBridges, platformMcpEnabled, servers, platformName, t_i18n]);

  return (
    <section className="space-y-4">
      {/* A section heading of the same weight as the page's own, not a card
          header: these endpoints are the opposite direction of travel from the
          servers listed below, and burying them in a card read as a widget on
          someone else's page rather than a subject of its own. */}
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-3xl font-bold">{t_i18n("MCP Endpoints")}</h1>
          <Badge variant="secondary" className="content-caption font-normal">
            {t_i18n("Read-only")}
          </Badge>
        </div>
        <p className="mt-2 text-default-secondary">
          {t_i18n("The MCP endpoints {platform} exposes to external clients (Claude, Cursor, Codex, ...). Connect one with a personal API key: every call runs with your own identity and permissions. Administrators configure these in Settings > Integrations > MCP Endpoint.", { platform: platformName || "XTM One" })}
        </p>
      </div>

      <Card>
      <CardContent className="p-4 space-y-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {isLoading && items.length === 0
            ? Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-9 rounded-sm" />
              ))
            : items.map((item) => (
                <Tip
                  key={item.key}
                  content={
                    item.detail
                      ? `${item.name} — ${item.status} · ${item.detail}`
                      : `${item.name} — ${item.status}`
                  }
                >
                  <div
                    className={cn(
                      "flex items-center gap-2 rounded-sm bg-elevation-default px-2.5 py-2",
                      item.tone === "disabled" && "opacity-60",
                    )}
                  >
                    {item.provider ? (
                      <ProviderLogo
                        provider={item.provider}
                        className="h-4 w-4 shrink-0"
                      />
                    ) : (
                      <Icon name="server" size={16} className="shrink-0 text-accent" />
                    )}
                    <span className="min-w-0 flex-1 truncate content-compact">
                      {item.name}
                    </span>
                    <span
                      aria-hidden
                      // The tone is published as data, not inferred from paint.
                      // The end-to-end suite needs to tell "not connected" from
                      // "disabled", and it was doing that by counting elements
                      // carrying a literal amber background class — which the
                      // design system migration repainted out from under it.
                      // State belongs in an attribute; the class is how it
                      // looks. (Naming the old class here would not help: the
                      // codemod rewrites comments too, precisely because they
                      // quote the classes they explain.)
                      data-tone={item.tone}
                      className={cn(
                        "h-2 w-2 shrink-0 rounded-full",
                        DOT_CLASS[item.tone],
                      )}
                    />
                    <span className="sr-only">{item.status}</span>
                  </div>
                </Tip>
              ))}
        </div>

        {showXtm && (
          <div className="border-t border-elevation-subtle-soft pt-3">
            <XtmMcpPackageDownload compact apiKeyCount={(apiKeys ?? []).length} />
          </div>
        )}
      </CardContent>
      </Card>
    </section>
  );
}
