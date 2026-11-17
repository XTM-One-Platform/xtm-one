import { AlertTriangle, Bot, Clock, Eye, Gauge, Server } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { formatCompactNumber } from "@/lib/utils";
import { API_KEY_QUOTA_USAGE_LABELS, API_KEY_SURFACE_LABELS, apiKeyState, daysUntil } from "@/lib/api-keys";
import { useFormatter } from "@/i18n";
import type { UserApiKey } from "@/types";

interface ApiKeyBadgesProps {
  apiKey: UserApiKey;
  /** The administrator's maximum lifetime, named by the beyond-maximum badge. */
  maxLifetimeDays: number;
  /** Read once by the list, so every row is judged against the same moment. */
  now: number;
}

/** A key's state, scopes, expiry and limits, as the profile, the user page and the admin list show them. */
export function ApiKeyBadges({ apiKey, maxLifetimeDays, now }: ApiKeyBadgesProps) {
  const { t_i18n, intl } = useFormatter();
  const state = apiKeyState(apiKey, now);
  const expiryDate = apiKey.expires_at
    ? intl.formatDate(apiKey.expires_at, { year: "numeric", month: "short", day: "numeric" })
    : null;
  const agentNames = apiKey.agents.map((agent) => agent.name ?? t_i18n("Unknown agent"));

  return (
    <div className="flex flex-wrap items-center gap-1">
      {apiKey.is_from_env && <Badge variant="outline">{t_i18n("Environment")}</Badge>}

      {state === "revoked" && <Badge variant="destructive">{t_i18n("Revoked")}</Badge>}
      {state === "expired" && (
        <Tip content={expiryDate ?? ""}>
          <Badge variant="destructive">{t_i18n("Expired")}</Badge>
        </Tip>
      )}
      {state === "expiring" && apiKey.expires_at && (
        <Tip content={expiryDate ?? ""}>
          <Badge variant="warning" className="gap-1">
            <Clock className="h-3 w-3" />
            {t_i18n("Expires in {count, plural, one {# day} other {# days}}", { count: daysUntil(apiKey.expires_at, now) })}
          </Badge>
        </Tip>
      )}
      {state === "active" && (
        <Badge variant="outline" className="gap-1">
          <Clock className="h-3 w-3" />
          {expiryDate ? t_i18n("Expires {date}", { date: expiryDate }) : t_i18n("No expiry")}
        </Badge>
      )}

      {apiKey.full_scope ? (
        <Tip content={t_i18n("Every surface, read and write, every agent its owner can use.")}>
          <Badge variant="secondary">{t_i18n("Full access")}</Badge>
        </Tip>
      ) : (
        <>
          {apiKey.all_surfaces ? (
            <Badge variant="secondary" className="gap-1">
              <Server className="h-3 w-3" />
              {t_i18n("All surfaces")}
            </Badge>
          ) : (
            apiKey.surfaces.map((surface) => (
              <Badge key={surface} variant="secondary" className="gap-1">
                <Server className="h-3 w-3" />
                {t_i18n(API_KEY_SURFACE_LABELS[surface])}
              </Badge>
            ))
          )}
          {apiKey.read_only && (
            <Badge variant="cyan" className="gap-1">
              <Eye className="h-3 w-3" />
              {t_i18n("Read-only")}
            </Badge>
          )}
          {apiKey.agent_ids !== null && (
            <Tip content={agentNames.join(", ")}>
              <Badge variant="secondary" className="gap-1">
                <Bot className="h-3 w-3" />
                {agentNames.length === 1
                  ? agentNames[0]
                  : t_i18n("{count, plural, one {# agent} other {# agents}}", { count: apiKey.agent_ids.length })}
              </Badge>
            </Tip>
          )}
        </>
      )}

      {apiKey.rate_limit_per_minute !== null && (
        <Badge variant="outline" className="gap-1">
          <Gauge className="h-3 w-3" />
          {t_i18n("{count} per minute", { count: formatCompactNumber(apiKey.rate_limit_per_minute) })}
        </Badge>
      )}
      {apiKey.quota_limit !== null && apiKey.quota_period && (
        <Tip content={t_i18n("Requests this key has made in the current period, out of its quota.")}>
          <Badge variant="outline">
            {t_i18n(API_KEY_QUOTA_USAGE_LABELS[apiKey.quota_period], {
              used: formatCompactNumber(apiKey.quota_used ?? 0),
              limit: formatCompactNumber(apiKey.quota_limit),
            })}
          </Badge>
        </Tip>
      )}

      {apiKey.beyond_max_lifetime && (
        <Tip
          content={t_i18n(
            "It outlives the maximum lifetime of {count, plural, one {# day} other {# days}} set since it was created. It keeps working: give it an expiry to bring it within.",
            { count: maxLifetimeDays },
          )}
        >
          <Badge variant="warning" className="gap-1">
            <AlertTriangle className="h-3 w-3" />
            {t_i18n("Beyond the maximum lifetime")}
          </Badge>
        </Tip>
      )}
    </div>
  );
}
