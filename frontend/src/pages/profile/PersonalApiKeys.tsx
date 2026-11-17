// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { BarChart3, Check, Copy, Key, Pencil, Plus, Trash2 } from "lucide-react";
import { ApiKeyBadges } from "@/components/api-keys/ApiKeyBadges";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { useNow } from "@/hooks/useTick";
import { useApiKeyPolicy, useApiKeys, useDeleteApiKey } from "@/hooks/api/useMiscApi";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { cn, formatCompactNumber } from "@/lib/utils";
import { apiKeyState } from "@/lib/api-keys";
import { useFormatter } from "@/i18n";
import type { UserApiKey } from "@/types";
import { ApiKeyDialog } from "./ApiKeyDialog";
import { formatTimeAgo } from "@/lib/relative-time";

/** The Profile's API keys: create, change, delete, and the secret shown once. */
export function PersonalApiKeys({ platformName }: { platformName: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data: apiKeys } = useApiKeys();
  const { data: policy } = useApiKeyPolicy();
  const deleteKey = useDeleteApiKey();
  const { isImpersonating } = useAuth();
  const [confirmElement, confirm] = useConfirm();
  const now = useNow();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<UserApiKey | null>(null);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const openDialog = (key: UserApiKey | null) => {
    setEditing(key);
    setDialogOpen(true);
  };

  const remove = async (key: UserApiKey) => {
    const ok = await confirm({
      title: t_i18n("Delete API key"),
      description: t_i18n(
        'Anything using "{name}" stops working at once. This cannot be undone.',
        { name: key.name },
      ),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (ok) deleteKey.mutate(key.id);
  };

  return (
    <div className="space-y-3">
      {confirmElement}
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="content-base font-semibold flex items-center gap-2">
            <Key className="h-4 w-4 text-accent" />
            {t_i18n("API Keys")}
          </h2>
          <p className="text-[11px] text-default-secondary mt-1">
            {t_i18n(
              "Create personal API keys to interact with {platform} programmatically. Each key gets its own surfaces, access, expiry and limits.",
              { platform: platformName || t_i18n("the platform") },
            )}
          </p>
        </div>
        <Tip content={isImpersonating ? t_i18n("You cannot create API key when impersonating") : t_i18n("New API key")}>
          <span>
            <Button
              size="sm"
              className="shrink-0 gap-1.5 h-8"
              onClick={() => openDialog(null)}
              disabled={isImpersonating}
            >
              <Plus className="h-3 w-3" />
              {t_i18n("Create Key")}
            </Button>
          </span>
        </Tip>
      </div>

      {newKey && (
        <div className="rounded-sm bg-feedback-success-primary/5 p-3 space-y-2">
          <p className="content-compact font-medium text-feedback-success-primary">
            {t_i18n("Key created: copy it now, it won't be shown again.")}
          </p>
          <div className="flex items-center gap-2">
            <code
              className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 text-[11px] text-accent font-mono-plex break-all select-all"
              data-testid="api-key-created-secret"
            >
              {newKey}
            </code>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 shrink-0"
              aria-label={t_i18n("Copy")}
              onClick={() => {
                navigator.clipboard.writeText(newKey);
                setCopied(true);
                setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
              }}
            >
              {copied ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" /> : <Copy className="h-3.5 w-3.5" />}
            </Button>
          </div>
          <Button variant="ghost" size="sm" className="content-caption h-6 px-2" onClick={() => setNewKey(null)}>
            {t_i18n("Dismiss")}
          </Button>
        </div>
      )}

      {apiKeys && apiKeys.length > 0 && (
        <div className="space-y-1.5">
          {apiKeys.map((k) => {
            const state = apiKeyState(k, now);
            const usable = state !== "expired" && state !== "revoked";
            return (
              <div
                key={k.id}
                className="flex items-start gap-3 rounded-sm bg-elevation-hover px-3 py-2.5"
                data-testid="api-key-row"
                data-key-name={k.name}
              >
                <Key className={cn("h-3.5 w-3.5 shrink-0 mt-0.5", usable ? "text-default-secondary" : "text-feedback-error-primary")} />
                <div className="min-w-0 flex-1 space-y-1.5">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <p className="content-compact font-medium truncate">{k.name}</p>
                    <p className="content-caption text-default-secondary font-mono-plex">{k.key_prefix}</p>
                  </div>
                  <ApiKeyBadges apiKey={k} maxLifetimeDays={policy?.max_lifetime_days ?? 0} now={now} />
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <Tip content={t_i18n("Total requests")}>
                    <div className="flex items-center gap-1 content-caption text-default-secondary">
                      <BarChart3 className="h-3 w-3" />
                      <span>{t_i18n("{count} requests", { count: formatCompactNumber(k.request_count) })}</span>
                    </div>
                  </Tip>
                  <span className="content-caption text-default-secondary">
                    {k.last_used_at ? t_i18n("Last used {time}", { time: formatTimeAgo(intl, k.last_used_at) }) : t_i18n("Never used")}
                  </span>
                </div>
                {k.is_from_env ? (
                  <Tip content={t_i18n("Provisioned by environment (non-deletable)")}>
                    <span>
                      <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0 text-default-secondary" disabled aria-label={t_i18n("Provisioned by environment (non-deletable)")}>
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </span>
                  </Tip>
                ) : (
                  <div className="flex shrink-0 items-center">
                    {k.is_active && (
                      <Tip content={isImpersonating ? t_i18n("You cannot change an API key while impersonating") : t_i18n("Edit")}>
                        <span>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 text-default-secondary hover:text-default-primary"
                            onClick={() => openDialog(k)}
                            disabled={isImpersonating}
                            aria-label={t_i18n("Edit")}
                          >
                            <Pencil className="h-3 w-3" />
                          </Button>
                        </span>
                      </Tip>
                    )}
                    <Tip content={isImpersonating ? t_i18n("You cannot delete an API key while impersonating") : t_i18n("Delete")}>
                      <span>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-default-secondary hover:text-feedback-error-primary"
                          onClick={() => remove(k)}
                          disabled={isImpersonating}
                          aria-label={t_i18n("Delete")}
                        >
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </span>
                    </Tip>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {apiKeys && apiKeys.length === 0 && !newKey && (
        <p className="content-compact text-default-secondary text-center py-2">
          {t_i18n("No API keys yet. Create one to get started.")}
        </p>
      )}

      <ApiKeyDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        apiKey={editing}
        onCreated={(created) => setNewKey(created.key)}
      />
    </div>
  );
}
