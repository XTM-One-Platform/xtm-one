// SPDX-License-Identifier: Apache-2.0

import { Suspense } from "react";
import { Bot, Wrench, Zap, Activity, ArrowUpRight, ArrowDownLeft } from "lucide-react";
import { StatCard } from "@/components/ui/stat-card";
import { useFormatter } from "@/i18n";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useAuth } from "@/hooks/useAuth";
import { useKBsByIntegration } from "@/hooks/api/useKnowledgeBaseApi";
import { useIntegrationToolList } from "@/hooks/api/useIntegrationApi";
import { formatShortDate, formatTime, isPerUserIntegration } from "@/lib/utils";
import { isA2AProvider, useA2APartnerActivity } from "@/hooks/api/useA2AApi";
import { useA2ADirection } from "@/hooks/api/useA2AClientApi";
import { StatusCard } from "./StatusCard";
import { YourAccountCard } from "./YourAccountCard";
import { ConnectedAccountsCard } from "./ConnectedAccountsCard";
import { KBsUsingCard } from "./KBsUsingCard";
import { visibleProviderOverviewCards } from "./provider-tabs";
import { WhoCanUseItCard } from "./WhoCanUseItCard";
import type { Integration, ProviderCatalogItem } from "@/types";

type Tab = "overview" | "config" | "activity";

export function OverviewTab({ integration, catalogEntry, onAuthorize, onConnectOwnAccount, connectingOwnAccount = false, setTab }: { integration: Integration; catalogEntry?: ProviderCatalogItem; onAuthorize?: () => void; onConnectOwnAccount?: () => void; connectingOwnAccount?: boolean; setTab: (tab: Tab) => void }) {
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const { data: agents } = useAgents({ scope: "all" });
  const { data: kbs } = useKBsByIntegration(integration.id, { scope: "all" });

  const boundAgents = agents?.filter((a) =>
    a.integration_bindings && Object.values(a.integration_bindings).flat().includes(integration.id),
  ) || [];

  const tools = useIntegrationToolList(integration, catalogEntry);
  const lastCalled = integration.last_called_at ? new Date(integration.last_called_at) : null;
  // This provider's surface is inbound protocol operations rather than
  // agent tools, so the tool cards do not apply to it.
  const isA2A = isA2AProvider(integration.provider);
  // An A2A integration's activity is its OWN partner's traffic, scoped by the
  // registration recorded on each task rather than by the agents it is bound
  // to; every other provider keeps the tool-call timestamp its counters
  // maintain.
  const { data: partnerActivity } = useA2APartnerActivity(
    integration.id,
    isA2A,
  );
  // Which way this partner relationship points, for the badge below: the
  // direction the integration states, so an inbound one never calls the
  // outbound-only verification endpoint (#11790). Absent until known: a
  // badge whose whole purpose is that an operator never has to infer the
  // direction must not state the opposite one first.
  const direction = useA2ADirection(integration) ?? null;

  const lastActivity = isA2A
    ? (partnerActivity?.last_activity_at
        ? new Date(partnerActivity.last_activity_at)
        : null)
    : lastCalled;

  return (
    <div className="space-y-6">
      {/* Direction, first and unmissable. It decides whether binding this to
          an agent EXPOSES that agent or gives it a tool — opposite
          consequences from one action, so an operator must never have to
          infer which they are configuring. */}
      {direction && (
        <div className="flex items-center gap-2 content-compact">
          {direction === "outbound" ? (
            <ArrowUpRight className="h-4 w-4 text-accent" />
          ) : (
            <ArrowDownLeft className="h-4 w-4 text-accent" />
          )}
          <span className="font-medium">
            {direction === "outbound"
              ? t_i18n("Outbound: our agents task this partner")
              : t_i18n("Inbound: this partner tasks our agents")}
          </span>
          <span className="text-default-secondary">
            {direction === "outbound"
              ? t_i18n("Binding it to an agent gives that agent a delegation tool.")
              : t_i18n("Binding it to an agent exposes that agent to the partner.")}
          </span>
        </div>
      )}

      {/* KPI cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard icon={Bot} label="Agents" value={boundAgents.length} subtitle={t_i18n("{count, plural, one {agent using this integration} other {agents using this integration}}", { count: boundAgents.length })} />
        {!isA2A && <StatCard icon={Wrench} label="Tools" value={tools.length} subtitle={t_i18n("{read, plural, other {# read}}, {write, plural, other {# write}}", { read: tools.filter((t) => t.tool_type === "read").length, write: tools.filter((t) => t.tool_type === "write").length })} onClick={() => setTab("config")} />}
        {/* Tool-call counting measures agent-side invocations, which never
            happen for a provider whose surface is inbound — so the counter
            card is omitted here rather than shown as a permanent zero. */}
        {!isA2A && (
          <StatCard icon={Zap} label="Tool calls" value={integration.call_count} subtitle={t_i18n("across {agents, plural, one {# agent} other {# agents}} · {kbs, plural, one {# KB} other {# KBs}} syncing", { agents: boundAgents.length, kbs: kbs?.length ?? 0 })} />
        )}
        <StatCard
          icon={Activity}
          label="Last activity"
          value={lastActivity ? formatShortDate(lastActivity) : "—"}
          subtitle={lastActivity
            ? formatTime(lastActivity)
            : (isA2A ? "No partner tasks yet" : "No calls yet")}
        />
      </div>

      {/* Connection + related entities */}
      <StatusCard integration={integration} catalogEntry={catalogEntry} onAuthorize={onAuthorize} />

      {/* Each person's own account: theirs to connect, for those who
          act with it; who connected one, for those who manage it. */}
      {isPerUserIntegration(integration) && integration.uses_own_credential && onConnectOwnAccount && (
        <YourAccountCard
          integration={integration}
          catalogEntry={catalogEntry}
          onAuthorize={onConnectOwnAccount}
          authorizing={connectingOwnAccount}
        />
      )}
      {isPerUserIntegration(integration) && integration.connected_user_count != null && (
        <ConnectedAccountsCard integration={integration} />
      )}

      {/* Cards the provider adds (provider-tabs.tsx), e.g. the platform LLM
          provider an OpenRouter integration feeds. */}
      {visibleProviderOverviewCards(integration, { userId: user?.id, isAdmin }).map((card) => (
        <Suspense key={card.key} fallback={null}>
          {card.render(integration)}
        </Suspense>
      ))}

      {/* One list of who reaches it and how: direct shares, then each bound
          agent once (with its orphan flag), then the other carriers. */}
      <WhoCanUseItCard integration={integration} agents={boundAgents} />

      <KBsUsingCard integrationId={integration.id} />
    </div>
  );
}
