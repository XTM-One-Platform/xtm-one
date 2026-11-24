// SPDX-License-Identifier: Apache-2.0

import { useNavigate } from "react-router-dom";
import { Spinner } from "@filigran/design-system";
import {
  ArrowRight,
  Bot,
  Building2,
  Database,
  FileText,
  MessageSquare,
  Share2,
  Sparkles,
  User as UserIcon,
  Users,
  Workflow,
  Wrench,
  Zap,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { describeAudience as describeAudienceText, lentResourceLabel, type LentKindName } from "@/lib/credential-exposure";
import { CHANNEL_TYPE_LABELS, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { useResourceExposure } from "@/hooks/api/useExposureApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import type { LentResourceKind, ResourceAudience, ResourceExposurePath, ResourceExposurePathKind } from "@/types";

type Translate = (key: string, values?: Record<string, string | number>) => string;

const describeAudience = (t: Translate, audience: ResourceAudience) =>
  describeAudienceText(t, audience, (channel) => CHANNEL_TYPE_LABELS[channel] ?? channel);

const PATH_ICONS: Record<ResourceExposurePathKind, typeof Bot> = {
  company: Building2,
  group: Users,
  people: UserIcon,
  agent: Bot,
  assignment: Zap,
  flow: Workflow,
  knowledge_base: Database,
  skill: Sparkles,
  prompt: FileText,
  custom_tool: Wrench,
  general_assistant: MessageSquare,
  personal_assistant: MessageSquare,
};

const DIRECT = new Set<ResourceExposurePathKind>(["company", "group", "people"]);

function pathLink(path: ResourceExposurePath): string | null {
  const id = path.entity?.id;
  if (!id || !path.entity?.visible) return null;
  switch (path.kind) {
    case "agent":
      return `/agents/${id}`;
    case "assignment":
      return `/agents/assignments/${id}`;
    case "flow":
      return `/flows/library/${id}`;
    case "knowledge_base":
      return `/knowledge/${id}`;
    case "skill":
      return `/skills/${id}`;
    case "prompt":
      return `/prompts/${id}`;
    case "custom_tool":
      return `/tools/${id}`;
    default:
      return null;
  }
}

const NOTE_TEXT: Record<string, string> = {
  chat_uses_own_integration:
    "In a chat, people who cannot see this integration use their own; the agent's runs, public link and channels use this one.",
  variable_resolves_for_viewers_and_runs:
    "It resolves for people who can see it, and in runs acting as someone who can.",
  account_not_shared: "Shared without the connected account: they connect their own.",
  per_user_resolves_to_acting_user:
    "A per-user binding: in a chat each person uses their own, while the assignment's runs use the integration of the person they act as, or the company-wide one that person may use.",
};

/**
 * "Sharing and access" for every lent resource: an integration, an MCP
 * server, a custom tool, a channel bot or a variable - to whom it is shared and
 * through what (directly, agents, assignments, flows, knowledge bases, skills,
 * prompts, custom tools, the General Assistant), with a link to each carrier
 * the viewer can open. Shown to the people who manage the resource (the
 * endpoint answers 403 otherwise, and the card then renders nothing).
 */
export function ResourceSharingCard({
  kind,
  id,
  enabled = true,
}: {
  kind: LentResourceKind;
  id: string;
  enabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data, isLoading, isError } = useResourceExposure(kind, id, enabled);

  if (!enabled || isError) return null;

  const reasonLabel = (reason: string): string => {
    switch (reason) {
      case "binding":
        return t_i18n("bound");
      case "pipeline":
        return t_i18n("pipeline step");
      case "assignment_override":
        return t_i18n("assignment override");
      case "trigger":
        return t_i18n("event trigger");
      case "flow_node":
        return t_i18n("flow node");
      case "sync_source":
        return t_i18n("import source");
      case "skill_source":
        return t_i18n("private source");
      case "per_user_template":
        return t_i18n("per-user binding");
      case "attached_prompt":
        return t_i18n("attached prompt");
      case "attached_skill":
        return t_i18n("attached skill");
      case "attached_knowledge_base":
        return t_i18n("attached knowledge base");
      case "channel":
        return t_i18n("channel");
      default:
        return t_i18n("referenced");
    }
  };

  const title = (path: ResourceExposurePath): string => {
    const entity = path.entity;
    switch (path.kind) {
      case "company":
        return path.restricted
          ? t_i18n("Company-managed, restricted to its audience")
          : t_i18n("Company-managed: every user");
      case "group":
        return t_i18n("Shared with the group {name}", { name: path.audience.groups[0]?.name ?? "" });
      case "people":
        return t_i18n("{count, plural, one {Shared with # person} other {Shared with # people}}", { count: path.audience.people_count });
      case "general_assistant":
        return t_i18n("The General Assistant's platform bindings");
      case "personal_assistant":
        return t_i18n("{name}'s own assistant", { name: entity?.name ?? "" });
      default:
        break;
    }
    const kindLabel: Record<string, string> = {
      agent: t_i18n("Agent"),
      assignment: t_i18n("Assignment"),
      flow: t_i18n("Flow"),
      knowledge_base: t_i18n("Knowledge base"),
      skill: t_i18n("Skill"),
      prompt: t_i18n("Prompt"),
      custom_tool: t_i18n("Custom tool"),
    };
    if (entity?.visible && entity.name) return `${kindLabel[path.kind]} - ${entity.name}`;
    const owner = entity?.owner_name ?? path.parent?.owner_name ?? "";
    return t_i18n("A {kind} you cannot open, of {owner}", { kind: kindLabel[path.kind] ?? path.kind, owner });
  };

  const secret = data?.secret;
  const lends = !!secret && secret.kind !== "none" && secret.kind !== "platform_managed";
  const holderLine = data && secret
    ? lends
      ? lentResourceLabel(t_i18n, {
          kind: kind as LentKindName,
          secretKind: secret.kind,
          name: data.resource.name ?? "",
          provider: INTEGRATION_PROVIDER_NAMES[data.resource.provider ?? ""] ?? data.resource.provider ?? "",
          isCompany: secret.is_company,
          isYours: secret.holder_id === user?.id,
          holderName: secret.holder_name,
        })
      : t_i18n("Nothing is stored in it that others act with.")
    : null;
  const indirect = data?.paths.filter((p) => !DIRECT.has(p.kind)) ?? [];

  return (
    <Card data-testid="resource-sharing-access">
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Share2 className="h-4 w-4 text-accent" />
          {t_i18n("Sharing and access")}
          {data && (
            <Badge variant="cyan" className="content-caption ml-auto">
              {t_i18n("{count, plural, one {# way} other {# ways}}", { count: data.paths.length })}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading || !data ? (
          <div className="flex justify-center py-6">
            <Spinner size="md" label={t_i18n("Loading...")} />
          </div>
        ) : (
          <>
            <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
              <p className="content-base font-medium">{holderLine}</p>
              {lends && (
                <p className="content-compact text-default-secondary">
                  {data.paths.length === 0
                    ? t_i18n("Not shared: only its owner uses it.")
                    : t_i18n("Can be used by {who}.", { who: describeAudience(t_i18n, data.reached) || t_i18n("its owner only") })}
                </p>
              )}
            </div>
            {data.paths.length === 0 ? (
              <p className="content-compact text-default-secondary">
                {t_i18n("Nothing shares it: no share, agent, assignment, flow, prompt or assistant uses it.")}
              </p>
            ) : (
              <ul className="space-y-2">
                {data.paths.map((path, index) => {
                  const Icon = PATH_ICONS[path.kind] ?? Share2;
                  const link = pathLink(path);
                  const body = (
                    <>
                      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-accent/10">
                        <Icon className="h-4 w-4 text-accent" />
                      </div>
                      <div className="min-w-0 flex-1 space-y-0.5">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <p className="content-base font-medium truncate">{title(path)}</p>
                          {path.reasons.map((reason) => (
                            <Badge key={reason} variant="outline" className="content-caption">
                              {reasonLabel(reason)}
                            </Badge>
                          ))}
                        </div>
                        <p className="text-[11px] text-default-secondary">
                          {path.usable
                            ? t_i18n("Usable by {who}.", { who: describeAudience(t_i18n, path.lent ?? path.audience) || t_i18n("nobody else") })
                            : t_i18n("Shared here without what it stores: they use their own.")}
                        </p>
                        {path.notes.filter((n) => NOTE_TEXT[n] && n !== "account_not_shared").map((note) => (
                          <p key={note} className="content-caption text-default-secondary">{t_i18n(NOTE_TEXT[note])}</p>
                        ))}
                      </div>
                      {link && <ArrowRight className="h-3.5 w-3.5 text-default-disabled group-hover:text-accent transition-colors shrink-0" />}
                    </>
                  );
                  return (
                    <li key={`${path.kind}:${path.entity?.id ?? index}`}>
                      {link ? (
                        <button
                          type="button"
                          onClick={() => navigate(link)}
                          className="w-full flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 text-left transition-colors hover:bg-elevation-highlight group"
                        >
                          {body}
                        </button>
                      ) : (
                        <div className="w-full flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">{body}</div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            {indirect.length > 0 && (
              <Tip content={t_i18n("Removing a share does not unbind it from these: open each one to change it.")}>
                <p className="text-[11px] text-default-secondary/80 w-fit">
                  {t_i18n("{count, plural, one {# entity uses it on behalf of its own audience.} other {# entities use it on behalf of their own audiences.}}", { count: indirect.length })}
                </p>
              </Tip>
            )}
            {data.truncated && (
              <p className="content-caption text-feedback-warning-primary">{t_i18n("Only the first entities of each kind are listed.")}</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
