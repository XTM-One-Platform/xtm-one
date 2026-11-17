// SPDX-License-Identifier: Apache-2.0

import type { ComponentProps } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Icon, Spinner } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { describeAudience as describeAudienceText, lentResourceLabel } from "@/lib/credential-exposure";
import { CHANNEL_TYPE_LABELS, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { canManageEntity } from "@/lib/entity-permissions";
import { cn } from "@/lib/utils";
import { useResourceExposure } from "@/hooks/api/useExposureApi";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import type { Agent, Integration, ResourceAudience, ResourceExposurePath } from "@/types";
import { agentAccessRows, bindingsWithout } from "./who-can-use-it";

type Translate = (key: string, values?: Record<string, string | number>) => string;
type GlyphName = ComponentProps<typeof Icon>["name"];

const describeAudience = (t: Translate, audience: ResourceAudience) =>
  describeAudienceText(t, audience, (channel) => CHANNEL_TYPE_LABELS[channel] ?? channel);

const DIRECT_ICONS = { company: "building-2", group: "users", people: "user" } as const;
type DirectKind = keyof typeof DIRECT_ICONS;
const isDirect = (path: ResourceExposurePath): path is ResourceExposurePath & { kind: DirectKind } =>
  path.kind in DIRECT_ICONS;

/**
 * "Who can use it" on an integration: ONE list of how people and agents reach
 * it, grouped by access path (direct shares, then agents bound to it, then the
 * other entities that carry it), with one total at the top. Replaces the
 * "Sharing and access" and "Agents Using This Integration" cards, which listed
 * every bound agent twice. An agent whose owner can no longer see the
 * integration is flagged: its runs refuse the integration's tools until the
 * owner's access comes back or the binding is removed (here, in one click).
 */
export function WhoCanUseItCard({ integration, agents }: { integration: Integration; agents: Agent[] }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const qc = useQueryClient();
  const { user, isAdmin } = useAuth();
  const manages = integration.can_manage !== false;
  const { data, isLoading, isError } = useResourceExposure("integration", integration.id, manages);
  const updateAgent = useUpdateAgent();
  // Unbinding saves the agent, an AI-gated route: with AI off it would only refuse.
  const aiOff = useAiActionDisabled();

  const exposure = manages && !isError ? data : undefined;
  const loading = manages && !isError && isLoading;
  const paths = exposure?.paths ?? [];
  const direct = paths.filter(isDirect);
  const agentRows = agentAccessRows(paths, agents);
  const others = paths.filter((p) => !isDirect(p) && p.kind !== "agent");
  const orphanedCount = agentRows.filter((r) => r.orphaned).length;

  const secret = exposure?.secret;
  const lends = !!secret && secret.kind !== "none" && secret.kind !== "platform_managed";
  const holderLine = exposure && secret
    ? lends
      ? lentResourceLabel(t_i18n, {
          kind: "integration",
          secretKind: secret.kind,
          name: exposure.resource.name ?? "",
          provider: INTEGRATION_PROVIDER_NAMES[exposure.resource.provider ?? ""] ?? exposure.resource.provider ?? "",
          isCompany: secret.is_company,
          isYours: secret.holder_id === user?.id,
          holderName: secret.holder_name,
        })
      : t_i18n("Nothing is stored in it that others act with.")
    : null;

  const unbind = (agent: Agent) =>
    updateAgent.mutate(
      { id: agent.id, integration_bindings: bindingsWithout(agent, integration.id) },
      {
        onSuccess: () => {
          void qc.invalidateQueries({ queryKey: ["exposure", "integration", integration.id] });
          toast({ title: t_i18n("{name} no longer uses this integration", { name: agent.name }), variant: "success" });
        },
      },
    );

  /** What runs with this integration's credential through an agent. */
  const runsLine = (agent: Agent, path: ResourceExposurePath | null): string => {
    const parts: string[] = [];
    const chatUsesOwn = path?.notes.includes("chat_uses_own_integration") ?? false;
    if (!agent.disable_chat) {
      parts.push(chatUsesOwn ? t_i18n("chats: each person uses their own") : t_i18n("chats"));
    }
    parts.push(t_i18n("{count, plural, =0 {no assignment} one {# assignment} other {# assignments}}", { count: agent.assignment_count }));
    if (agent.web_enabled) parts.push(t_i18n("its public link"));
    const channels = Object.keys(agent.channel_config ?? {}).map((c) => CHANNEL_TYPE_LABELS[c] ?? c);
    if (channels.length) parts.push(t_i18n("channels: {names}", { names: channels.join(", ") }));
    return t_i18n("Runs with it: {parts}.", { parts: parts.join(", ") });
  };

  const directTitle = (path: ResourceExposurePath & { kind: DirectKind }): string => {
    if (path.kind === "company") {
      return path.restricted ? t_i18n("Company-managed, restricted to its audience") : t_i18n("Company-managed: every user");
    }
    if (path.kind === "group") {
      const group = path.audience.groups[0];
      return t_i18n("{count, plural, one {Group {name} (# member)} other {Group {name} (# members)}}", {
        name: group?.name ?? "",
        count: group?.member_count ?? path.audience.people_count,
      });
    }
    return t_i18n("{count, plural, one {Shared with # person} other {Shared with # people}}", { count: path.audience.people_count });
  };

  const rowClass = "w-full flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5";
  const iconTile = (name: GlyphName, warn = false) => (
    <div className={cn("flex h-8 w-8 shrink-0 items-center justify-center rounded-sm", warn ? "bg-alert-warning" : "bg-accent/10")}>
      <Icon name={name} size={16} className={warn ? "text-feedback-warning-primary" : "text-accent"} />
    </div>
  );

  return (
    <Card data-testid="integration-who-can-use-it">
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Icon name="key-round" size={16} className="text-accent" />
          {t_i18n("Who can use it")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <div className="flex justify-center py-6">
            <Spinner size="md" label={t_i18n("Loading...")} />
          </div>
        ) : (
          <>
            {exposure && (
              <div className="rounded-sm bg-elevation-hover p-3 space-y-1" data-testid="who-can-use-it-total">
                <p className="content-base font-medium">{holderLine}</p>
                {lends && (
                  <p className="content-compact text-default-secondary">
                    {paths.length === 0
                      ? t_i18n("Not shared: only its owner uses it.")
                      : t_i18n("Can be used by {who}.", { who: describeAudience(t_i18n, exposure.reached) || t_i18n("its owner only") })}
                  </p>
                )}
                {orphanedCount > 0 && (
                  // The warning hue carries the icon only (3:1 for a glyph): as text it
                  // reads 3.84:1 on this surface in light, under the 4.5:1 floor.
                  <p className="flex items-start gap-1.5 content-compact text-default-primary" data-testid="who-can-use-it-orphaned-total">
                    <Icon name="triangle-alert" size={16} className="mt-0.5 shrink-0 text-feedback-warning-primary" aria-hidden />
                    <span>
                      {t_i18n("{count, plural, one {# agent's owner no longer has access: its runs refuse this integration's tools.} other {# agents' owners no longer have access: their runs refuse this integration's tools.}}", { count: orphanedCount })}
                    </span>
                  </p>
                )}
              </div>
            )}

            {exposure && (
              <section className="space-y-2" aria-labelledby="who-direct">
                <h3 id="who-direct" className="content-compact font-medium text-default-secondary">{t_i18n("Direct shares")}</h3>
                {direct.length === 0 ? (
                  <p className="content-compact text-default-secondary">{t_i18n("Not shared with anyone directly.")}</p>
                ) : (
                  <ul className="space-y-2">
                    {direct.map((path, index) => (
                      <li key={`${path.kind}:${path.audience.groups[0]?.id ?? index}`} className={rowClass}>
                        {iconTile(DIRECT_ICONS[path.kind])}
                        <div className="min-w-0 flex-1 space-y-0.5">
                          <p className="content-base font-medium truncate">{directTitle(path)}</p>
                          <p className="content-caption text-default-secondary">
                            {path.usable
                              ? t_i18n("Usable by {who}.", { who: describeAudience(t_i18n, path.lent ?? path.audience) || t_i18n("nobody else") })
                              : t_i18n("Shared without the connected account: they connect their own.")}
                          </p>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}

            <section className="space-y-2" aria-labelledby="who-agents">
              <h3 id="who-agents" className="content-compact font-medium text-default-secondary">{t_i18n("Agents bound to it")}</h3>
              {agentRows.length === 0 ? (
                <p className="content-compact text-default-secondary">{t_i18n("Bind this integration to an agent to start using its tools.")}</p>
              ) : (
                <ul className="space-y-2">
                  {agentRows.map((row) => {
                    const link = row.agent || row.path?.entity?.visible ? `/agents/${row.agent?.id ?? row.path?.entity?.id}` : null;
                    const canUnbind = row.orphaned && !!row.agent && canManageEntity(row.agent, user?.id, isAdmin);
                    // The whole row opens the agent, as the knowledge base rows of this
                    // page do; "Unbind" stays a separate control beside it.
                    const body = (
                      <>
                        {iconTile(row.orphaned ? "triangle-alert" : "bot", row.orphaned)}
                        <div className="min-w-0 flex-1 space-y-0.5">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <p className="content-base font-medium truncate">{row.name || t_i18n("An agent you cannot open")}</p>
                            {row.orphaned && (
                              <Badge variant="warning" className="content-caption">{t_i18n("Owner no longer has access")}</Badge>
                            )}
                            {row.agent && !row.agent.enabled && (
                              <Badge variant="outline" className="content-caption">{t_i18n("Disabled")}</Badge>
                            )}
                          </div>
                          {row.ownerName && (
                            <p className="content-caption text-default-secondary">{t_i18n("Owner: {name}", { name: row.ownerName })}</p>
                          )}
                          {row.orphaned ? (
                            <p className="content-caption text-default-secondary">
                              {t_i18n("Its runs refuse this integration's tools until its owner gets access back or the agent is unbound.")}
                            </p>
                          ) : (
                            <>
                              {row.path && (
                                <p className="content-caption text-default-secondary">
                                  {t_i18n("Reaches {who}.", { who: describeAudience(t_i18n, row.path.lent ?? row.path.audience) || t_i18n("nobody else") })}
                                </p>
                              )}
                              {row.agent && <p className="content-caption text-default-secondary">{runsLine(row.agent, row.path)}</p>}
                            </>
                          )}
                        </div>
                        {link && <Icon name="arrow-right" size={16} className="text-default-disabled group-hover:text-accent transition-colors shrink-0" />}
                      </>
                    );
                    return (
                      <li
                        key={row.key}
                        className={cn("flex items-center gap-3 rounded-sm bg-elevation-hover", link && "transition-colors hover:bg-elevation-highlight", canUnbind && "pr-3")}
                        data-testid="who-can-use-it-agent"
                      >
                        {link ? (
                          <button
                            type="button"
                            onClick={() => navigate(link)}
                            className="group flex min-w-0 flex-1 items-center gap-3 rounded-sm px-3 py-2.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                            data-testid="who-can-use-it-agent-open"
                          >
                            {body}
                          </button>
                        ) : (
                          <div className="flex min-w-0 flex-1 items-center gap-3 px-3 py-2.5">{body}</div>
                        )}
                        {canUnbind && row.agent && (
                          <AiDisabledTip>
                            <Button variant="outline" disabled={updateAgent.isPending || aiOff} onClick={() => unbind(row.agent as Agent)}>
                              {t_i18n("Unbind")}
                            </Button>
                          </AiDisabledTip>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>

            {others.length > 0 && (
              <section className="space-y-2" aria-labelledby="who-others">
                <h3 id="who-others" className="content-compact font-medium text-default-secondary">{t_i18n("Through other entities")}</h3>
                <ul className="space-y-2">
                  {others.map((path, index) => (
                    <li key={`${path.kind}:${path.entity?.id ?? index}`} className={rowClass}>
                      {iconTile("share-2")}
                      <div className="min-w-0 flex-1 space-y-0.5">
                        <p className="content-base font-medium truncate">
                          {path.entity?.visible && path.entity.name
                            ? path.entity.name
                            : t_i18n("An entity you cannot open, of {owner}", { owner: path.entity?.owner_name ?? path.parent?.owner_name ?? "" })}
                        </p>
                        <p className="content-caption text-default-secondary">
                          {t_i18n("Reaches {who}.", { who: describeAudience(t_i18n, path.lent ?? path.audience) || t_i18n("nobody else") })}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {exposure?.truncated && (
              <p className="content-caption text-feedback-warning-primary">{t_i18n("Only the first entities of each kind are listed.")}</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
