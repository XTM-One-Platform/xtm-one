import { useState } from "react";
import { KeyRound, Pencil, Plus, Trash2, TriangleAlert } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CompactFigure } from "@/components/ui/compact-figure";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import {
  useCreateOpenRouterKey,
  useDeleteOpenRouterKey,
  useOpenRouterKeys,
  useOpenRouterOverview,
  useUpdateOpenRouterKey,
} from "@/hooks/api/useOpenRouterApi";
import { useFormatter } from "@/i18n";
import type { Integration, OpenRouterApiKey, OpenRouterKeyUse, OpenRouterLimitReset } from "@/types";
import { LIMIT_RESET_LABELS, exactUsd, expiryFromDateInput, formatUsd } from "./openrouter-console";
import { ConsoleErrorNote, ManagementKeyNeeded } from "./OpenRouterUsageTab";

/** What runs on a key, as a badge; every value is a catalog key `validate-i18n` checks. @i18n-keys */
const KEY_USE_LABELS = {
  integration: "This integration",
  llm_provider: "Platform LLM provider",
} as const satisfies Record<OpenRouterKeyUse, string>;

const NO_RESET = "__none";

/** Width floor of the Name column: the fixed layout keeps the key names readable (slice 7 of #9378). */
export const KEY_NAME_COLUMN_WIDTH = 240;

/**
 * Width of every other column under the fixed layout (UX round 1 on #10601, B1): with a
 * width on each column the table keeps one row height and scrolls sideways inside its
 * card on a narrow screen instead of squeezing the columns until badges and dates wrap.
 */
export const KEY_COLUMN_WIDTHS = {
  used_by: 180,
  money: 96,
  limit: 180,
  date: 120,
  enabled: 88,
} as const;

interface KeyDraft {
  name: string;
  limit: string;
  limitReset: OpenRouterLimitReset | typeof NO_RESET;
  includeByok: boolean;
  expires: string;
}

const draftOf = (key?: OpenRouterApiKey): KeyDraft => ({
  name: key?.name ?? "",
  limit: key?.limit === null || key?.limit === undefined ? "" : String(key.limit),
  limitReset: key?.limit_reset ?? NO_RESET,
  includeByok: key?.include_byok_in_limit ?? false,
  expires: "",
});

/** The limit field as the API takes it: blank = no limit, else a non-negative amount. */
function parsedLimit(text: string): number | null | "invalid" {
  if (!text.trim()) return null;
  const value = Number(text);
  return Number.isFinite(value) && value >= 0 ? value : "invalid";
}

function KeyDialog({
  open, editing, onClose, onSubmit, busy,
}: {
  open: boolean;
  editing: OpenRouterApiKey | null;
  onClose: () => void;
  onSubmit: (draft: KeyDraft, limit: number | null) => void;
  busy: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [draft, setDraft] = useState<KeyDraft>(() => draftOf(editing ?? undefined));
  const limit = parsedLimit(draft.limit);
  const invalid = !draft.name.trim() || limit === "invalid";
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{editing ? t_i18n("Edit API key") : t_i18n("Create an API key")}</DialogTitle>
          <DialogDescription>
            {editing
              ? t_i18n("The changes apply on OpenRouter right away.")
              : t_i18n("The key is created on the OpenRouter account and shown once: copy it before you close the dialog that follows.")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="openrouter-key-name">{t_i18n("Name")}</Label>
            <Input aria-label={t_i18n("Name")} aria-describedby="openrouter-key-name-help" id="openrouter-key-name" value={draft.name} maxLength={200} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            <p id="openrouter-key-name-help" className="content-caption text-default-secondary">{t_i18n("Shown in the key list here and on OpenRouter.")}</p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="openrouter-key-limit">{t_i18n("Spending limit (USD)")}</Label>
              <Input aria-label={t_i18n("Spending limit (USD)")}
                id="openrouter-key-limit"
                aria-describedby="openrouter-key-limit-help"
                aria-invalid={limit === "invalid" || undefined}
                inputMode="decimal"
                placeholder={t_i18n("No limit")}
                value={draft.limit}
                onChange={(e) => setDraft({ ...draft, limit: e.target.value })}
              />
              {limit === "invalid"
                ? <p id="openrouter-key-limit-help" className="content-caption text-feedback-error-primary">{t_i18n("Enter an amount of 0 or more.")}</p>
                : <p id="openrouter-key-limit-help" className="content-caption text-default-secondary">{t_i18n("Requests stop once the key has spent this much; empty means no limit.")}</p>}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="openrouter-key-reset">{t_i18n("Limit resets")}</Label>
              <Select value={draft.limitReset} onValueChange={(value) => setDraft({ ...draft, limitReset: value as KeyDraft["limitReset"] })}>
                <SelectTrigger id="openrouter-key-reset" aria-describedby="openrouter-key-reset-help" className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_RESET}>{t_i18n("Never")}</SelectItem>
                  {(Object.keys(LIMIT_RESET_LABELS) as OpenRouterLimitReset[]).map((reset) => (
                    <SelectItem key={reset} value={reset}>{t_i18n(LIMIT_RESET_LABELS[reset])}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p id="openrouter-key-reset-help" className="content-caption text-default-secondary">{t_i18n("When the spend counted against the limit goes back to zero.")}</p>
            </div>
          </div>
          <label className="flex items-center justify-between gap-3 content-compact">
            <span>{t_i18n("Count bring-your-own-key spend in the limit")}</span>
            <Switch checked={draft.includeByok} onCheckedChange={(includeByok) => setDraft({ ...draft, includeByok })} />
          </label>
          {!editing && (
            <div className="space-y-1.5">
              <Label id="openrouter-key-expires-label" htmlFor="openrouter-key-expires">{t_i18n("Expires on (optional)")}</Label>
              <DateFilterInput
                id="openrouter-key-expires"
                aria-labelledby="openrouter-key-expires-label"
                aria-describedby="openrouter-key-expires-help"
                className="w-full"
                value={draft.expires}
                onChange={(expires) => setDraft({ ...draft, expires })}
              />
              <p id="openrouter-key-expires-help" className="content-caption text-default-secondary">{t_i18n("The key stops working at the end of this day; empty keeps it until you delete it.")}</p>
            </div>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose} disabled={busy}>{t_i18n("Cancel")}</Button>
            <Button disabled={invalid || busy} onClick={() => limit !== "invalid" && onSubmit(draft, limit)}>
              {editing ? t_i18n("Save") : t_i18n("Create key")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SecretDialog({ secret, onClose }: { secret: string | null; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Dialog open={!!secret} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t_i18n("Copy the new key now")}</DialogTitle>
          <DialogDescription>
            {t_i18n("OpenRouter shows it once and this platform does not keep it: once this dialog is closed it cannot be read again.")}
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2 rounded-sm bg-elevation-hover px-3 py-2">
          <code className="min-w-0 flex-1 break-all font-mono-plex content-compact">{secret}</code>
          {secret && <CopyIconButton value={secret} label={t_i18n("Copy the key")} />}
        </div>
        <div className="flex justify-end">
          <Button onClick={onClose}>{t_i18n("I have copied it")}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// OpenRouter API keys - sub-page under /integrations/:id/keys: the keys of the
// account, through its management key. A key this platform runs on (this
// integration's, the platform LLM provider's) cannot be disabled or deleted.
// ---------------------------------------------------------------------------
export function OpenRouterKeysTab({ integration }: { integration: Integration }) {
  const { t_i18n, intl } = useFormatter();
  const usd = (value: number | null | undefined) => formatUsd(value, intl.locale);
  // The exact amount on hover once compacted; undefined when the drawn figure is already exact.
  const exact = (value: number | null | undefined) => exactUsd(value, intl.locale);
  // The full limit line on hover, the exact amounts once compacted (UX round 1 on #10601, B2).
  const limitTitle = (remaining: number | null | undefined, limit: number | null | undefined) =>
    t_i18n("{remaining} left of {limit}", { remaining: exact(remaining) ?? usd(remaining), limit: exact(limit) ?? usd(limit) });
  // A date in the user's locale, its relative form on hover (UX round 2 on #10601, B1).
  const dateCell = (iso: string) => {
    const at = Date.parse(iso);
    if (Number.isNaN(at)) return <span className="whitespace-nowrap text-default-secondary">{iso}</span>;
    const days = Math.round((at - Date.now()) / 86_400_000);
    return (
      <Tip content={intl.formatRelativeTime(days, "day", { numeric: "auto" })}>
        <span className="whitespace-nowrap text-default-secondary">{intl.formatDate(at, { dateStyle: "medium" })}</span>
      </Tip>
    );
  };
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const aiOff = useAiActionDisabled();
  const overview = useOpenRouterOverview(integration.id, !aiOff);
  const hasManagementKey = !!overview.data?.has_management_key;
  const keys = useOpenRouterKeys(integration.id, !aiOff && hasManagementKey);
  const create = useCreateOpenRouterKey(integration.id);
  const update = useUpdateOpenRouterKey(integration.id);
  const remove = useDeleteOpenRouterKey(integration.id);
  const [dialog, setDialog] = useState<{ editing: OpenRouterApiKey | null } | null>(null);
  const [secret, setSecret] = useState<string | null>(null);

  if (aiOff) return <AiDisabledNotice />;
  if (overview.isLoading) return <PageSkeleton cards={2} />;
  if (overview.error || !overview.data) {
    return <ConsoleErrorNote error={(overview.error as Error) ?? { message: t_i18n("The OpenRouter account could not be read.") }} />;
  }
  const canManage = overview.data.can_manage_keys;
  const failed = (title: string) => (error: Error) => toast({ title: t_i18n(title), description: error.message, variant: "error" });
  const busy = create.isPending || update.isPending || remove.isPending;

  const submit = (draft: KeyDraft, limit: number | null) => {
    const limitReset = draft.limitReset === NO_RESET ? null : draft.limitReset;
    if (dialog?.editing) {
      update.mutate(
        { hash: dialog.editing.hash, name: draft.name.trim(), limit, limit_reset: limitReset, include_byok_in_limit: draft.includeByok },
        {
          onSuccess: () => {
            setDialog(null);
            toast({ title: t_i18n("API key updated"), variant: "success" });
          },
          onError: failed("The key could not be updated"),
        },
      );
      return;
    }
    create.mutate(
      {
        name: draft.name.trim(),
        limit,
        limit_reset: limitReset,
        include_byok_in_limit: draft.includeByok,
        expires_at: expiryFromDateInput(draft.expires),
      },
      {
        onSuccess: (created) => {
          setDialog(null);
          setSecret(created.secret);
        },
        onError: failed("The key could not be created"),
      },
    );
  };

  const setDisabled = (key: OpenRouterApiKey, disabled: boolean) =>
    update.mutate(
      { hash: key.hash, disabled },
      { onError: failed(disabled ? "The key could not be disabled" : "The key could not be enabled") },
    );

  const doDelete = async (key: OpenRouterApiKey) => {
    const confirmed = await confirm({
      title: "Delete this API key?",
      description: "Everything that calls OpenRouter with it fails from now on. This cannot be undone.",
      confirmLabel: "Delete",
    });
    if (!confirmed) return;
    remove.mutate(key.hash, {
      onSuccess: () => toast({ title: t_i18n("API key deleted"), variant: "success" }),
      onError: failed("The key could not be deleted"),
    });
  };

  const lockReasonOf = (key: OpenRouterApiKey) =>
    key.in_use.length > 0 ? t_i18n("This platform runs on this key: replace it there first.") : undefined;
  const money = (value: number) => <CompactFigure exact={exact(value) ?? usd(value)}>{usd(value)}</CompactFigure>;

  const columns: DataTableColumn<OpenRouterApiKey>[] = [
    {
      key: "name",
      header: "Name",
      width: KEY_NAME_COLUMN_WIDTH,
      cell: (key) => (
        <div className="min-w-0">
          <Tip content={key.name || undefined}>
            <p className="truncate font-medium">{key.name || "—"}</p>
          </Tip>
          <p className="truncate font-mono-plex content-caption text-default-secondary">{key.label}</p>
        </div>
      ),
      sortValue: (key) => key.name.toLowerCase(),
      firstSort: "asc",
      csv: (key) => key.name,
    },
    {
      key: "used_by",
      header: "Used by",
      hideable: true,
      width: KEY_COLUMN_WIDTHS.used_by,
      // One badge on one line, the others counted as "+N" with every use on hover (B1).
      cell: (key) => {
        if (key.in_use.length === 0) return <span className="text-default-secondary">—</span>;
        const labels = key.in_use.map((use) => t_i18n(KEY_USE_LABELS[use]));
        return (
          <Tip content={labels.join(", ")}>
            <div className="flex min-w-0 items-center gap-1" data-testid="openrouter-key-uses">
              <Badge variant="cyan" className="min-w-0 truncate whitespace-nowrap">{labels[0]}</Badge>
              {labels.length > 1 && (
                <span className="shrink-0 content-caption text-default-secondary">{`+${labels.length - 1}`}</span>
              )}
            </div>
          </Tip>
        );
      },
      sortValue: (key) => key.in_use.length,
      csv: (key) => key.in_use.map((use) => KEY_USE_LABELS[use]).join(", "),
    },
    {
      key: "monthly",
      header: "This month",
      hideable: true,
      align: "right",
      width: KEY_COLUMN_WIDTHS.money,
      cell: (key) => money(key.usage_monthly),
      sortValue: (key) => key.usage_monthly,
      csv: (key) => key.usage_monthly,
    },
    {
      key: "usage",
      header: "Total",
      hideable: true,
      align: "right",
      width: KEY_COLUMN_WIDTHS.money,
      cell: (key) => money(key.usage),
      sortValue: (key) => key.usage,
      csv: (key) => key.usage,
    },
    {
      key: "limit",
      header: "Limit",
      hideable: true,
      width: KEY_COLUMN_WIDTHS.limit,
      cell: (key) =>
        key.limit === null ? (
          <span className="text-default-secondary">{t_i18n("No limit")}</span>
        ) : (
          <div className="min-w-0">
            <Tip content={limitTitle(key.limit_remaining, key.limit)}>
              <p className="truncate" data-testid="openrouter-key-limit">
                {t_i18n("{remaining} left of {limit}", { remaining: usd(key.limit_remaining), limit: usd(key.limit) })}
              </p>
            </Tip>
            {key.limit_reset && (
              <Tip content={t_i18n(LIMIT_RESET_LABELS[key.limit_reset])}>
                <p className="truncate content-caption text-default-secondary">{t_i18n(LIMIT_RESET_LABELS[key.limit_reset])}</p>
              </Tip>
            )}
          </div>
        ),
      sortValue: (key) => (key.limit === null ? null : key.limit_remaining),
      csv: (key) => key.limit,
    },
    {
      key: "created",
      header: "Created",
      hideable: true,
      width: KEY_COLUMN_WIDTHS.date,
      cell: (key) => (key.created_at ? dateCell(key.created_at) : <span className="text-default-secondary">—</span>),
      sortValue: (key) => key.created_at,
      csv: (key) => key.created_at,
    },
    {
      key: "expires",
      header: "Expires",
      hideable: true,
      width: KEY_COLUMN_WIDTHS.date,
      cell: (key) =>
        key.expires_at ? dateCell(key.expires_at) : <span className="whitespace-nowrap text-default-secondary">{t_i18n("Never")}</span>,
      sortValue: (key) => key.expires_at,
      csv: (key) => key.expires_at,
    },
    {
      key: "enabled",
      header: "Enabled",
      width: KEY_COLUMN_WIDTHS.enabled,
      interactive: true,
      cell: (key) => (
        <Tip content={!key.disabled ? lockReasonOf(key) : undefined}>
          <span>
            <Switch
              checked={!key.disabled}
              disabled={!canManage || busy || (!key.disabled && key.in_use.length > 0)}
              onCheckedChange={(enabled) => setDisabled(key, !enabled)}
              aria-label={t_i18n("Enabled")}
            />
          </span>
        </Tip>
      ),
      sortValue: (key) => !key.disabled,
      csv: (key) => !key.disabled,
    },
  ];

  const filters: DataTableFilter<OpenRouterApiKey>[] = [
    {
      key: "status",
      label: "Status",
      values: (key) => [key.disabled ? "disabled" : "enabled"],
      options: [
        { value: "enabled", label: t_i18n("Enabled") },
        { value: "disabled", label: t_i18n("Disabled") },
      ],
    },
    {
      key: "used_by",
      label: "Used by",
      values: (key) => key.in_use,
      options: (Object.keys(KEY_USE_LABELS) as OpenRouterKeyUse[]).map((use) => ({ value: use, label: t_i18n(KEY_USE_LABELS[use]) })),
    },
  ];

  const keyActions = (key: OpenRouterApiKey) => (
    <span className="inline-flex gap-1">
      <Button variant="ghost" size="icon" aria-label={t_i18n("Edit API key")} onClick={() => setDialog({ editing: key })} disabled={busy}>
        <Pencil className="h-4 w-4" />
      </Button>
      <Tip content={lockReasonOf(key)}>
        <span>
          <Button
            variant="ghost"
            size="icon"
            className="text-feedback-error-primary hover:bg-feedback-error-primary/10 hover:text-feedback-error-primary"
            aria-label={t_i18n("Delete API key")}
            onClick={() => void doDelete(key)}
            disabled={busy || key.in_use.length > 0}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </span>
      </Tip>
    </span>
  );

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2 text-base">
                <KeyRound className="h-4 w-4 text-accent" />
                {t_i18n("API keys of the account")}
              </CardTitle>
              <CardDescription>
                {t_i18n("Give each team, environment or tool its own key with its own spending limit, and disable a key without touching the others.")}
              </CardDescription>
            </div>
            {hasManagementKey && canManage && (
              <Button size="sm" className="gap-1.5" onClick={() => setDialog({ editing: null })} disabled={busy}>
                <Plus className="h-3.5 w-3.5" />
                {t_i18n("Create a key")}
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {!hasManagementKey ? (
            <ManagementKeyNeeded integration={integration} what="Listing and managing the account's API keys needs a management key." />
          ) : keys.isLoading ? (
            <PageSkeleton cards={2} />
          ) : keys.error ? (
            <ConsoleErrorNote error={keys.error as Error} />
          ) : (
            <>
              {!canManage && (
                <p className="content-compact text-default-secondary">
                  {t_i18n("Only the holder of this OpenRouter account - an administrator for a company-managed integration, its owner otherwise - can create, edit or delete its keys.")}
                </p>
              )}
              {keys.data?.truncated && (
                <p className="flex items-center gap-1.5 content-compact text-feedback-warning-primary">
                  <TriangleAlert className="h-3.5 w-3.5" />
                  {t_i18n("The account holds more keys than are listed here.")}
                </p>
              )}
                <DataTable
                  rows={keys.data?.keys ?? []}
                  columns={columns}
                  layout="fixed"
                  rowKey={(key) => key.hash}
                  defaultSort={{ key: "name", direction: "asc" }}
                  searchFields={(key) => [key.name, key.label]}
                  searchPlaceholder={t_i18n("Search...")}
                  filters={filters}
                  exportName="openrouter-api-keys"
                  urlKey="ork_"
                  storageKey="openrouter-api-keys"
                  emptyLabel={t_i18n("No API key on this account yet.")}
                  rowTestId="openrouter-key-row"
                  rowActions={canManage ? keyActions : undefined}
                />
            </>
          )}
        </CardContent>
      </Card>
      {dialog && (
        <KeyDialog
          key={dialog.editing?.hash ?? "new"}
          open
          editing={dialog.editing}
          onClose={() => setDialog(null)}
          onSubmit={submit}
          busy={busy}
        />
      )}
      <SecretDialog secret={secret} onClose={() => setSecret(null)} />
      {ConfirmEl}
    </div>
  );
}
