// SPDX-License-Identifier: Apache-2.0

import { Share2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { useResourceExposureSummary } from "@/hooks/api/useExposureApi";
import type { LentResourceKind, ResourceExposurePathKind, ResourceExposureSummary } from "@/types";

const ORDER: ResourceExposurePathKind[] = [
  "company",
  "group",
  "people",
  "agent",
  "assignment",
  "flow",
  "knowledge_base",
  "skill",
  "prompt",
  "custom_tool",
  "general_assistant",
  "personal_assistant",
];

/** What the people it reaches act with, for every lent resource. */
function secretNote(t: (k: string) => string, summary: ResourceExposureSummary): string {
  const kind = summary.secret_kind;
  switch (kind) {
    case "none":
    case "platform_managed":
      return t("No personal credential is stored.");
    case "code":
      return t("Everyone above runs its code.");
    case "secret_value":
      return t("Everyone above uses its secret value, never shown to them.");
    case "plain_value":
      return t("Everyone above uses its value.");
    case "mcp_headers":
    case "mcp_env":
    case "mcp_oauth_token":
      return summary.is_company
        ? t("Everyone above calls it with what the company stored in it.")
        : t("Everyone above calls it with what its owner stored in it.");
    default:
      if (summary.is_company) return t("Everyone above acts with the company's credential.");
      if (kind === "oauth_account" && !summary.account_shared) return t("The connected account itself is not shared.");
      return t("Everyone above acts with its owner's personal credential.");
  }
}

/**
 * "Shared - 3 ways" on an integration card, with the breakdown in a `Tip`:
 * directly (company-wide, groups, people) and through what uses it
 * (agents, assignments, flows, knowledge bases, skills, assistants). Renders
 * nothing for an integration shared no way at all.
 */
export function SharingWaysBadge({
  summary,
  size = "sm",
}: {
  summary?: ResourceExposureSummary;
  /** Text is `content-compact` (12 px) at both sizes; "sm" keeps a smaller icon for tight card rows. */
  size?: "sm" | "default";
}) {
  const { t_i18n } = useFormatter();
  if (!summary || summary.ways === 0) return null;
  const counts = summary.counts as Partial<Record<ResourceExposurePathKind, number>>;
  const line = (kind: ResourceExposurePathKind, count: number): string => {
    switch (kind) {
      case "company":
        return t_i18n("Company-managed: every user");
      case "group":
        return t_i18n("{count, plural, one {Shared with # group} other {Shared with # groups}}", { count });
      case "people":
        return t_i18n("{count, plural, one {Shared with # person} other {Shared with # people}}", { count });
      case "agent":
        return t_i18n("{count, plural, one {Used by # agent} other {Used by # agents}}", { count });
      case "assignment":
        return t_i18n("{count, plural, one {Used by # assignment} other {Used by # assignments}}", { count });
      case "flow":
        return t_i18n("{count, plural, one {Used by # flow} other {Used by # flows}}", { count });
      case "knowledge_base":
        return t_i18n("{count, plural, one {Syncs # knowledge base} other {Syncs # knowledge bases}}", { count });
      case "skill":
        return t_i18n("{count, plural, one {Used by # skill} other {Used by # skills}}", { count });
      case "prompt":
        return t_i18n("{count, plural, one {Referenced by # prompt} other {Referenced by # prompts}}", { count });
      case "custom_tool":
        return t_i18n("{count, plural, one {Referenced by # custom tool} other {Referenced by # custom tools}}", { count });
      case "general_assistant":
        return t_i18n("Bound to the General Assistant for every user");
      case "personal_assistant":
        return t_i18n("{count, plural, one {In # person's own assistant} other {In # people's own assistants}}", { count });
    }
  };
  const lines = ORDER.filter((kind) => counts[kind]).map((kind) => line(kind, counts[kind] ?? 0));
  if (summary.public_link) lines.push(t_i18n("Reachable through a public chat link"));
  const credentialNote = secretNote(t_i18n, summary);
  return (
    <Tip
      content={
        <div className="space-y-0.5 text-left">
          {lines.map((l) => (
            <p key={l}>{l}</p>
          ))}
          <p className="pt-1 opacity-80">{credentialNote}</p>
        </div>
      }
    >
      <Badge
        variant="outline"
        className="content-compact border-feedback-alert-primary/40 text-feedback-warning-primary gap-0.5"
        data-testid="sharing-ways-badge"
      >
        <Share2 className={size === "sm" ? "h-2.5 w-2.5" : "h-3 w-3"} />
        {t_i18n("{count, plural, one {Shared - # way} other {Shared - # ways}}", { count: summary.ways })}
      </Badge>
    </Tip>
  );
}

/**
 * The same indicator for any lent resource's card, fed by
 * `GET /exposure/{resources}/summary`: one request per resource kind, shared by
 * every card of the list (TanStack Query caches it). Nothing for a resource the
 * viewer does not manage, or shared no way at all.
 */
export function ResourceSharingWaysBadge({ kind, id }: { kind: LentResourceKind; id: string }) {
  const { data } = useResourceExposureSummary(kind);
  return <SharingWaysBadge summary={data?.summaries[id]} />;
}