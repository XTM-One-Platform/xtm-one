import { BarChart3, Ban, Key } from "lucide-react";
import { Link } from "react-router-dom";
import { ApiKeyBadges } from "@/components/api-keys/ApiKeyBadges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { apiKeyState } from "@/lib/api-keys";
import { cn, formatCompactNumber, timeAgo } from "@/lib/utils";
import type { AdminApiKey } from "@/types";
import { Spinner } from "@filigran/design-system";

export function AdminApiKeyRow({
  apiKey,
  maxLifetimeDays,
  now,
  showOwner,
  revoking,
  onRevoke,
}: {
  apiKey: AdminApiKey;
  maxLifetimeDays: number;
  now: number;
  showOwner: boolean;
  revoking: boolean;
  onRevoke: () => void;
}) {
  const { t_i18n } = useFormatter();
  const state = apiKeyState(apiKey, now);
  const dead = state === "revoked" || state === "expired";
  const ownerLabel = apiKey.owner_name ? `${apiKey.owner_name} (${apiKey.owner_email})` : apiKey.owner_email;

  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-sm border px-4 py-3 transition-colors",
        dead ? "border-elevation-subtle-soft layer-1 bg-elevation-default opacity-70" : "border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-xs",
      )}
      data-testid="admin-api-key-row"
      data-key-name={apiKey.name}
    >
      <div className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-sm", dead ? "bg-elevation-hover" : "bg-accent/10")}>
        <Key className={cn("h-4 w-4", dead ? "text-default-secondary" : "text-accent")} />
      </div>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="truncate content-base font-medium">{apiKey.name}</p>
          <code className="font-mono-plex content-caption text-default-secondary">{apiKey.key_prefix}</code>
          <ApiKeyBadges apiKey={apiKey} maxLifetimeDays={maxLifetimeDays} now={now} />
        </div>
        <div className="flex flex-wrap items-center gap-3 content-caption text-default-secondary">
          {showOwner && (
            <span className="inline-flex items-center gap-1.5">
              <Link to={`/users-and-groups/users/${apiKey.owner_id}`} className="font-medium text-default-secondary hover:text-accent hover:underline">
                {ownerLabel}
              </Link>
              {!apiKey.owner_active && (
                <Badge variant="outline" className="text-[9px]">
                  {t_i18n("Deactivated")}
                </Badge>
              )}
            </span>
          )}
          <span className="inline-flex items-center gap-1">
            <BarChart3 className="h-3 w-3" /> {t_i18n("{count} requests", { count: formatCompactNumber(apiKey.request_count) })}
          </span>
          <span>{apiKey.last_used_at ? t_i18n("Last used {time}", { time: timeAgo(apiKey.last_used_at) }) : t_i18n("Never used")}</span>
          <span>{t_i18n("Created {time}", { time: timeAgo(apiKey.created_at) })}</span>
        </div>
      </div>
      {apiKey.is_from_env ? (
        <Tip content={t_i18n("Provisioned by environment (non-deletable)")}>
          <span>
            <Button variant="ghost" size="sm" className="h-8 gap-1.5 content-compact" disabled>
              <Ban className="h-3.5 w-3.5" />
              {t_i18n("Revoke")}
            </Button>
          </span>
        </Tip>
      ) : (
        apiKey.is_active && (
          <Button
            variant="ghost"
            size="sm"
            className="h-8 gap-1.5 content-compact text-default-secondary hover:text-feedback-error-primary"
            onClick={onRevoke}
            disabled={revoking}
            data-testid="admin-api-key-revoke" aria-busy={revoking || undefined}>
            {revoking && <Spinner size="sm" tone="inherit" />}{!revoking && <Ban className="h-3.5 w-3.5" />}
            {t_i18n("Revoke")}
          </Button>
        )
      )}
    </div>
  );
}
