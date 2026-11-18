// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Access and security > Provisioning (#122).
 *
 * SCIM 2.0: the endpoint URL an identity provider (Microsoft Entra) is given,
 * its bearer tokens (generated here, shown once, stored hashed, revocable) and
 * the latest SCIM writes. Below, the review list of every deactivation: what
 * the cascade moved, disabled or kept, each item marked done by an
 * administrator. Contract: dev-docs/scim.md, dev-docs/user-deactivation.md.
 */
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import {
  useCreateScimToken,
  useDeactivations,
  useResolveReviewItem,
  useRevokeScimToken,
  useScimActivity,
  useScimTokens,
  type DeactivationEpisode,
  type ReviewResolution,
  type ScimTokenCreated,
} from "@/hooks/api/useProvisioningApi";
import { useFormatter } from "@/i18n";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import {
  DEACTIVATION_SOURCE_LABELS,
  labelOf as label,
  REVIEW_ACTION_LABELS,
  REVIEW_DETAIL_LABELS,
} from "@/lib/provisioning-labels";
import { formatShortDateTime } from "@/lib/utils";
import { Icon, Spinner } from "@filigran/design-system";

function useCopy() {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (key: string, value: string) => {
    void navigator.clipboard.writeText(value);
    setCopied(key);
    window.setTimeout(() => setCopied(null), COPY_FEEDBACK_DELAY);
  };
  return { copied, copy };
}

function ScimCard() {
  const { t_i18n } = useFormatter();
  const [ConfirmEl, confirm] = useConfirm();
  const { data, isLoading } = useScimTokens();
  const create = useCreateScimToken();
  const revoke = useRevokeScimToken();
  const [name, setName] = useState("Microsoft Entra");
  const [created, setCreated] = useState<ScimTokenCreated | null>(null);
  const { copied, copy } = useCopy();
  const active = (data?.tokens ?? []).filter((token) => !token.revoked_at);

  const handleRevoke = async (id: string, tokenName: string) => {
    const ok = await confirm({
      title: t_i18n("Revoke the SCIM token {name}?", { name: tokenName }),
      description: t_i18n("The identity provider that uses it can no longer create, update or deactivate accounts until it is given a new token."),
      confirmLabel: t_i18n("Revoke token"),
      variant: "danger",
    });
    if (ok) revoke.mutate(id, { onSuccess: () => setCreated((c) => (c?.id === id ? null : c)) });
  };

  return (
    <Card data-testid="scim-section">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon name="network" size={16} aria-hidden />
          {t_i18n("SCIM provisioning")}
        </CardTitle>
        <CardDescription>
          {t_i18n("An identity provider such as Microsoft Entra creates, updates and deactivates accounts here. Give it the endpoint URL and a token generated below.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1">
          <p className="content-compact text-default-secondary">{t_i18n("Endpoint URL")}</p>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 break-all rounded-sm bg-elevation-hover px-3 py-2 font-mono-plex content-compact" data-testid="scim-endpoint">
              {data?.endpoint ?? ""}
            </code>
            <Button variant="outline" size="sm" className="shrink-0 gap-1.5" onClick={() => data && copy("endpoint", data.endpoint)} disabled={!data}>
              {copied === "endpoint" ? <Icon name="check" size={16} aria-hidden /> : <Icon name="copy" size={16} aria-hidden />}
              {copied === "endpoint" ? t_i18n("Copied") : t_i18n("Copy")}
            </Button>
          </div>
        </div>

        {created && (
          <div className="space-y-2 rounded-sm border border-feedback-success-primary/20 bg-feedback-success-primary/10 p-4" data-testid="scim-token-reveal">
            <p className="content-compact font-medium text-feedback-success-primary">
              {t_i18n("Token generated: paste it into the identity provider now, it won't be shown again.")}
            </p>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 break-all rounded-sm bg-elevation-hover px-3 py-2 font-mono-plex content-compact">{created.token}</code>
              <Button variant="outline" size="sm" className="shrink-0 gap-1.5" onClick={() => copy("token", created.token)}>
                {copied === "token" ? <Icon name="check" size={16} aria-hidden /> : <Icon name="copy" size={16} aria-hidden />}
                {copied === "token" ? t_i18n("Copied") : t_i18n("Copy")}
              </Button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          <p className="content-compact text-default-secondary">{t_i18n("Tokens")}</p>
          {isLoading && <Spinner size="sm" />}
          {!isLoading && active.length === 0 && (
            <p className="content-compact text-default-secondary">{t_i18n("No token yet: SCIM is off until one is generated.")}</p>
          )}
          {active.map((token) => (
            <div key={token.id} className="flex flex-wrap items-center justify-between gap-3 rounded-sm bg-elevation-default p-3" data-testid="scim-token-row">
              <div className="min-w-0">
                <p className="truncate content-base font-medium" title={token.name}>{token.name}</p>
                <code className="font-mono-plex content-compact text-default-secondary">{token.token_prefix}</code>
              </div>
              <div className="flex items-center gap-3">
                <span className="content-compact text-default-secondary">
                  {token.last_used_at
                    ? t_i18n("Last used on {date}", { date: formatShortDateTime(token.last_used_at) })
                    : t_i18n("Never used")}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary"
                  onClick={() => void handleRevoke(token.id, token.name)}
                  disabled={revoke.isPending}
                >
                  <Icon name="trash-2" size={16} aria-hidden />
                  {t_i18n("Revoke")}
                </Button>
              </div>
            </div>
          ))}
        </div>

        <SettingsField
          id="scim-token-name"
          label={t_i18n("Token name")}
          helper={t_i18n("One token per identity provider, so you can revoke one without stopping the others.")}
          info={t_i18n(
            "The name you will recognise in the token list and in the audit trail, for example Microsoft Entra production. The token itself is shown once, right after you generate it.",
          )}
        >
          {/* items-start: the counter the Input draws under itself never pushes the button down. */}
          <div className="flex flex-wrap items-start gap-2">
            <Input
              id="scim-token-name"
              aria-describedby={settingsFieldHintId("scim-token-name")}
              className="max-w-xs"
              value={name}
              maxLength={255}
              onChange={(e) => setName(e.target.value)}
            />
            <Button
              className="gap-1.5"
              onClick={() => create.mutate(name.trim(), { onSuccess: setCreated })}
              disabled={create.isPending || !name.trim()}
              data-testid="scim-token-generate"
              aria-busy={create.isPending || undefined}
            >
              {create.isPending && <Spinner size="sm" tone="inherit" />}
              {!create.isPending && <Icon name="key-round" size={16} aria-hidden />}
              {t_i18n("Generate token")}
            </Button>
          </div>
        </SettingsField>
        {ConfirmEl}
      </CardContent>
    </Card>
  );
}

function ActivityCard() {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useScimActivity();
  return (
    <Card data-testid="scim-activity">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon name="history" size={16} aria-hidden />
          {t_i18n("Latest SCIM changes")}
        </CardTitle>
        <CardDescription>{t_i18n("The last 50 accounts and groups the identity provider created, changed or deactivated. Refused calls are not listed.")}</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading && <Spinner size="sm" />}
        {!isLoading && (data?.length ?? 0) === 0 && (
          <p className="content-compact text-default-secondary">{t_i18n("No SCIM change yet.")}</p>
        )}
        {(data?.length ?? 0) > 0 && (
          <ul className="divide-y divide-[var(--border-elevation-subtle)]">
            {data!.map((row) => (
              <li key={row.id} className="flex items-baseline justify-between gap-3 py-2">
                <span className="min-w-0 flex-1 truncate content-compact" title={row.summary}>{row.summary}</span>
                <span className="shrink-0 content-compact text-default-secondary">
                  {row.created_at ? formatShortDateTime(row.created_at) : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function EpisodeRow({ episode }: { episode: DeactivationEpisode }) {
  const { t_i18n } = useFormatter();
  const resolve = useResolveReviewItem();
  const open = episode.review_items.filter((item) => !item.reviewed_at);
  const mark = (key: string, resolution: ReviewResolution) =>
    resolve.mutate({ episodeId: episode.id, key, resolution });
  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle p-3" data-testid="deactivation-episode">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="min-w-0 truncate content-base font-medium" title={episode.user_email ?? ""}>
          {episode.user_name || episode.user_email}
        </p>
        <div className="flex items-center gap-2">
          {episode.deprovisioned && <Badge variant="outline" className="content-caption">{t_i18n("Removed by the identity provider")}</Badge>}
          <span className="content-compact text-default-secondary">
            {t_i18n(label(DEACTIVATION_SOURCE_LABELS, episode.source, "Platform"))}
            {episode.deactivated_at ? `, ${formatShortDateTime(episode.deactivated_at)}` : ""}
          </span>
        </div>
      </div>
      {open.length === 0 ? (
        <p className="content-compact text-default-secondary">{t_i18n("Everything was reviewed.")}</p>
      ) : (
        <ul className="space-y-2">
          {open.map((item) => (
            <li key={item.key} className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0 flex-1">
                <p className="truncate content-compact" title={item.name}>
                  <Badge variant="outline" className="mr-2 content-caption">{t_i18n(label(REVIEW_ACTION_LABELS, item.action, "Moved"))}</Badge>
                  {item.name}
                </p>
                {item.params && item.action in REVIEW_DETAIL_LABELS && (
                  <p className="content-compact text-default-secondary">
                    {t_i18n(REVIEW_DETAIL_LABELS[item.action as keyof typeof REVIEW_DETAIL_LABELS], item.params)}
                  </p>
                )}
              </div>
              <div className="flex gap-1">
                <Button variant="outline" size="sm" onClick={() => mark(item.key, "keep")} disabled={resolve.isPending}>
                  {t_i18n("Mark as kept")}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => mark(item.key, "reassign")} disabled={resolve.isPending}>
                  {t_i18n("Mark as reassigned")}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => mark(item.key, "delete")} disabled={resolve.isPending}>
                  {t_i18n("Mark as deleted")}
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ReviewCard() {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useDeactivations();
  const pending = (data ?? []).filter((episode) => episode.open_review_count > 0);
  return (
    <Card data-testid="deactivation-review">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon name="list-checks" size={16} aria-hidden />
          {t_i18n("Deactivation review")}
        </CardTitle>
        <CardDescription>
          {t_i18n("What deactivating an account moved, disabled or kept. Mark each item once you have checked it; the account's group memberships are kept for audit.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading && <Spinner size="sm" />}
        {!isLoading && pending.length === 0 && (
          <p className="content-compact text-default-secondary">{t_i18n("Nothing to review.")}</p>
        )}
        {pending.map((episode) => (
          <EpisodeRow key={episode.id} episode={episode} />
        ))}
      </CardContent>
    </Card>
  );
}

export function ProvisioningSection() {
  return (
    <div className="space-y-6">
      <ScimCard />
      <ReviewCard />
      <ActivityCard />
    </div>
  );
}
