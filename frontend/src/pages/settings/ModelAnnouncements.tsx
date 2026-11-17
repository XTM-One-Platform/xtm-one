// SPDX-License-Identifier: Apache-2.0

import { useCallback, useMemo, useState } from "react";
import { AlertTriangle, Check, Mail, MailCheck, Minus, Plus, RotateCcw, Send, Users, X } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import {
  useAnnounceDefaultModel,
  useAnnounceModelChanges,
  useModelAnnouncementCatalogs,
  useModelAnnouncementPreview,
  useModelAnnouncementStatus,
  useSendTestModelAnnouncement,
  type ModelAnnouncementEmailBody,
} from "@/hooks/api/useSettingsApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useDebounce } from "@/hooks/useDebounce";
import { useEmailingToast } from "@/hooks/useEmailingToast";
import { useFormatter } from "@/i18n";
import { PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import {
  buildDirectory,
  canEmailUsers,
  deliveryCounts,
  exceedsLimit,
  groupByProvider,
  heldUntil,
  inFlightStatus,
  isEmptySelection,
  MAX_ANNOUNCED_MODELS,
  sameSelection,
  selectionBody,
  selectionOf,
  selectionWarnings,
  withoutModel,
  type AnnouncedKind,
  type DeferredRetry,
  type ModelSelection,
} from "@/lib/model-announcements";
import { cn, formatShortDateTime, formatTime } from "@/lib/utils";
import type { CatalogModelRef, ModelAnnouncementStatus, ModelAnnouncementSummary, ModelCatalogChange } from "@/types";
import { ModelDirectory, type ProviderCatalogsState } from "./ModelDirectory";
import { Spinner } from "@filigran/design-system";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];
/** The right-hand pane of the model changes editor. */
type Pane = "selection" | "preview";

function modelIds(models: readonly CatalogModelRef[]): string {
  return models.map((m) => m.id).join(", ");
}

const IN_FLIGHT: ModelAnnouncementSummary["status"][] = ["pending", "sending"];

/** Why the users could not be emailed right now, or `null` when they can. */
function emailBlocker(status: ModelAnnouncementStatus, t_i18n: Translate): string | null {
  if (status.emails_disabled) {
    return t_i18n("Model announcement emails are turned off on this platform (MODEL_ANNOUNCEMENT_EMAILS_DISABLED), so the users cannot be emailed.");
  }
  if (!status.email_configured) {
    return t_i18n("Email is not configured on this platform (SMTP_HOSTNAME), so the users cannot be emailed.");
  }
  if (status.recipient_count === 0) {
    return t_i18n("There is no user to email: only active users other than the bootstrap administrator get it.");
  }
  return null;
}

/** An announcement being emailed: what holds it back, or how far it got and how long it still takes. */
function inFlightLine(latest: ModelAnnouncementSummary, emailsPerMinute: number, t_i18n: Translate): string {
  const status = inFlightStatus(latest, emailsPerMinute);
  if (status.kind === "queued") {
    const queued = t_i18n("Queued: emailed once the {count, plural, one {announcement} other {# announcements}} recorded before it went out.", { count: status.count });
    if (!status.blockedUntil) return queued;
    return `${queued} ${t_i18n("{count, plural, one {That announcement} other {The oldest of them}} waits until {time}.", {
      count: status.count,
      time: formatTime(status.blockedUntil),
    })}`;
  }
  if (status.kind === "held") {
    return t_i18n("Waiting for the deployed models to stop changing: emailed from {time}. A model change deployed before then is added to this email.", { time: formatTime(status.until) });
  }
  const progress = t_i18n("Emailing the users: {done} of {total} so far.", { done: status.done, total: status.total });
  let paused: string | null = null;
  if (status.paused) {
    paused = status.paused.when === "at"
      ? t_i18n("Paused after an email server error, next attempt at {time}.", { time: formatTime(status.paused.time) })
      : t_i18n("Paused after an email server error, retrying now.");
  }
  const left = status.minutesLeft === null
    ? null
    : t_i18n("About {count, plural, one {# minute} other {# minutes}} left, {rate} emails a minute.", {
      count: status.minutesLeft,
      rate: emailsPerMinute,
    });
  return [progress, paused, left, deferredLine(status.deferred, t_i18n)].filter(Boolean).join(" ");
}

/** The addresses the email server refused for now and when they are retried, or `null` when there are none. */
function deferredLine(deferred: DeferredRetry | null, t_i18n: Translate): string | null {
  if (!deferred) return null;
  const { count, retry } = deferred;
  if (retry.when === "after_others") {
    return t_i18n("{count, plural, one {# address refused it for now} other {# addresses refused it for now}}: retried once everyone else is emailed.", { count });
  }
  if (retry.when === "at") {
    return t_i18n("{count, plural, one {# address refused it for now} other {# addresses refused it for now}}: retried once at {time}.", { count, time: formatTime(retry.time) });
  }
  return t_i18n("{count, plural, one {# address refused it for now} other {# addresses refused it for now}}: retrying now.", { count });
}

function outcomeLine(latest: ModelAnnouncementSummary, emailsPerMinute: number, t_i18n: Translate): string {
  const date = formatShortDateTime(latest.sent_at ?? latest.created_at);
  switch (latest.status) {
    case "pending":
    case "sending":
      return inFlightLine(latest, emailsPerMinute, t_i18n);
    case "sent": {
      const sent = t_i18n("Emailed on {date} to {count, plural, one {# user} other {# users}}.", {
        date,
        count: latest.delivered,
      });
      return latest.refused > 0
        ? `${sent} ${t_i18n("{count, plural, one {# address refused it.} other {# addresses refused it.}}", { count: latest.refused })}`
        : sent;
    }
    case "skipped":
      if (latest.skip_reason === "disabled") {
        return t_i18n("Not emailed ({date}): MODEL_CATALOG_EMAILS_ENABLED was off when this change was deployed.", { date });
      }
      if (latest.skip_reason === "turned_off") {
        return t_i18n("Not emailed ({date}): model announcement emails were turned off (MODEL_ANNOUNCEMENT_EMAILS_DISABLED).", { date });
      }
      if (latest.skip_reason === "smtp_not_configured") {
        return t_i18n("Not emailed ({date}): email was not configured (SMTP_HOSTNAME).", { date });
      }
      if (latest.skip_reason === "no_recipients") {
        return t_i18n("Not emailed ({date}): there was no user to email.", { date });
      }
      return t_i18n("Not emailed ({date}).", { date });
    case "expired":
      return t_i18n("Not emailed ({date}): it could not be sent within 7 days.", { date });
    case "failed": {
      if (latest.failed_attempts === 0) {
        return t_i18n("Not emailed ({date}): every address refused it.", { date });
      }
      const { done, total } = deliveryCounts(latest);
      return t_i18n("Sending stopped ({date}) after {count, plural, one {# failed attempt} other {# failed attempts}} in a row: {done} of {total} users emailed.", {
        date,
        count: latest.failed_attempts,
        done,
        total,
      });
    }
    default:
      return "";
  }
}

/** The share of the users an announcement being emailed has reached. */
function DeliveryProgress({ latest }: { latest: ModelAnnouncementSummary }) {
  const { t_i18n } = useFormatter();
  const { done, total } = deliveryCounts(latest);
  if (total === 0 || latest.queued_behind > 0 || heldUntil(latest)) return null;
  const percent = Math.round((done / total) * 100);
  return (
    <div
      role="progressbar"
      aria-label={t_i18n("Users emailed")}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      className="h-1 w-full max-w-xs overflow-hidden rounded-full bg-elevation-hover"
    >
      <div
        className={cn("h-full rounded-full transition-all", latest.retry_at ? "bg-feedback-alert-primary" : "bg-accent")}
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}

/** The SMTP error behind a paused or failed announcement, else the server's last reply to an address it refused for now. */
function LastError({ latest }: { latest: ModelAnnouncementSummary }) {
  const { t_i18n } = useFormatter();
  if (latest.last_error) {
    return (
      <p className="break-words content-caption leading-relaxed text-feedback-warning-primary">
        {t_i18n("Last email server error: {error}", { error: latest.last_error })}
      </p>
    );
  }
  if (!latest.last_refusal) return null;
  return (
    <p className="break-words text-[11px] leading-relaxed text-default-secondary">
      {t_i18n("Last address refused: {error}", { error: latest.last_refusal })}
    </p>
  );
}

/** The models a change adds and the ones it retires, as badges. */
function ModelChanges({ added, removed }: { added: string[]; removed: string[] }) {
  const { t_i18n } = useFormatter();
  return (
    <>
      {added.length > 0 && (
        <span className="inline-flex flex-wrap items-center gap-1">
          {t_i18n("Added")}
          {added.map((m) => (
            <Badge key={m} variant="outline" className="px-1.5 py-0 content-caption font-medium">{m}</Badge>
          ))}
        </span>
      )}
      {removed.length > 0 && (
        <span className="inline-flex flex-wrap items-center gap-1">
          {t_i18n("Retired")}
          {removed.map((m) => (
            <Badge key={m} variant="secondary" className="px-1.5 py-0 content-caption font-medium line-through opacity-70">{m}</Badge>
          ))}
        </span>
      )}
    </>
  );
}

/** The Settings section each announced capability is chosen in. @i18n-keys */
const CAPABILITY_LABELS = {
  image: "Image Generation",
  video: "Video Generation",
  speech: "Speech Generation",
  music: "Music Generation",
  decision: "Decision Model",
} as const;

/** What the announcement said: the new default, the models added, the models retired, the capabilities. */
export function AnnouncedModels({ latest }: { latest: ModelAnnouncementSummary }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-default-secondary">
      {latest.default_model && (
        <span className="inline-flex items-center gap-1">
          {t_i18n("Default")}
          <Badge variant="cyan" className="px-1.5 py-0 content-caption font-medium">{latest.default_model}</Badge>
          {latest.previous_default_model && latest.previous_default_model !== latest.default_model && (
            <span>{t_i18n("replaces {model}", { model: latest.previous_default_model })}</span>
          )}
        </span>
      )}
      <ModelChanges
        added={latest.added_models.filter((m) => m !== latest.default_model)}
        removed={latest.removed_models}
      />
      {(latest.capabilities ?? []).map((item) => (
        <span key={item.capability} className="inline-flex items-center gap-1">
          {t_i18n(CAPABILITY_LABELS[item.capability] ?? item.capability)}
          <Badge variant="outline" className="px-1.5 py-0 content-caption font-medium">{item.model}</Badge>
          {item.previous_model && <span>{t_i18n("replaces {model}", { model: item.previous_model })}</span>}
        </span>
      ))}
    </div>
  );
}

/** Who gets the email, or why nobody can get it right now. */
function RecipientsNote({ status }: { status: ModelAnnouncementStatus }) {
  const { t_i18n } = useFormatter();
  const blocker = emailBlocker(status, t_i18n);
  if (blocker) {
    return (
      <div role="note" className="flex items-start gap-2.5 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/5 p-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" />
        <p className="content-base leading-relaxed text-feedback-warning-primary">{blocker}</p>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-2.5 rounded-sm bg-elevation-hover p-3">
      <Users className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
      <p className="content-base leading-relaxed text-default-secondary">
        {t_i18n("{count, plural, one {# active user gets the email.} other {# active users get the email.}} The bootstrap administrator account is never emailed.", { count: status.recipient_count })}
      </p>
    </div>
  );
}

/** "Send me a test email": the calling admin gets the email the users would get. */
function TestEmailControl({
  body,
  busy,
  disabled = false,
}: {
  body: ModelAnnouncementEmailBody;
  busy: boolean;
  /** There is nothing valid to announce yet. */
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  // Portalled outside the Settings fieldset, so it gates its own action.
  const aiOff = useAiActionDisabled();
  const sendTest = useSendTestModelAnnouncement();
  return (
    <div className="space-y-1.5">
      <AiDisabledTip wrapClassName="inline-flex">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1.5 px-2 content-compact"
          disabled={aiOff || sendTest.isPending || busy || disabled}
          onClick={() => sendTest.mutate(body)} aria-busy={sendTest.isPending || undefined}>
          {sendTest.isPending && <Spinner size="sm" tone="inherit" />}{!sendTest.isPending && <Send className="h-3 w-3" />}
          {t_i18n("Send me a test email")}
        </Button>
      </AiDisabledTip>
      {sendTest.isSuccess && (
        <p role="status" className="flex items-center gap-1.5 content-compact text-feedback-success-primary">
          <MailCheck className="h-3.5 w-3.5" />
          {t_i18n("Test email sent to {email}. Check it before emailing the users.", { email: sendTest.data.sent_to })}
        </p>
      )}
      {sendTest.isError && (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The test email was not sent: {error}", { error: sendTest.error?.message ?? "" })}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DefaultModelAnnouncementDialog — tell the users about the default model
// ---------------------------------------------------------------------------
/**
 * `change`: the admin is making `model` the default and chooses whether the
 * users are emailed with it. `announce`: the users are emailed about the
 * current default, env-locked included.
 */
export function DefaultModelAnnouncementDialog({
  mode,
  model,
  modelLabel,
  provider,
  previousModel,
  status,
  saving = false,
  onSave,
  onClose,
}: {
  mode: "change" | "announce";
  model: string;
  modelLabel: string;
  provider: string | null;
  previousModel: string | null;
  status: ModelAnnouncementStatus;
  saving?: boolean;
  /** `change` only: save the new default, emailing the users when `announce`. */
  onSave?: (announce: boolean) => void;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const announce = useAnnounceDefaultModel();
  const emailingToast = useEmailingToast();
  const blocker = emailBlocker(status, t_i18n);
  const busy = saving || announce.isPending;
  const replaces = previousModel && previousModel !== model ? previousModel : null;

  const emailUsers = () => {
    if (mode === "change") {
      onSave?.(true);
      return;
    }
    announce.mutate(undefined, {
      onSuccess: () => {
        emailingToast(status);
        onClose();
      },
    });
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/10">
              <Mail className="h-5 w-5 text-accent" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">
                {mode === "change"
                  ? t_i18n("Make {model} the default model?", { model: modelLabel })
                  : t_i18n("Email the users about {model}?", { model: modelLabel })}
              </DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {mode === "change"
                  ? replaces
                    ? t_i18n("Agents without a model of their own will use it instead of {previous}. You can also email your users that it is their new default model.", { previous: replaces })
                    : t_i18n("Agents without a model of their own will use it. You can also email your users that it is their new default model.")
                  : replaces
                    ? t_i18n("Every user gets the email saying it is their default model, replacing {previous}.", { previous: replaces })
                    : t_i18n("Every user gets the email saying it is their default model.")}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-3 pt-1">
          <RecipientsNote status={status} />
          {canEmailUsers(status) && (
            <TestEmailControl body={mode === "change" ? { model, provider } : {}} busy={busy} />
          )}
          {announce.isError && (
            <p role="alert" className="content-base text-feedback-error-primary">
              {t_i18n("The users were not emailed: {error}", { error: announce.error?.message ?? "" })}
            </p>
          )}
        </div>

        <div className="flex flex-wrap justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            {t_i18n("Cancel")}
          </Button>
          {mode === "change" && (
            <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={() => onSave?.(false)} aria-busy={saving || undefined}>
              {saving && <Spinner size="sm" tone="inherit" />}{!saving && <Check className="h-3 w-3" />}
              {t_i18n("Save without email")}
            </Button>
          )}
          <AiDisabledTip>
            <Button size="sm" className="gap-1.5" disabled={aiOff || busy || !!blocker} onClick={emailUsers} aria-busy={busy || undefined}>
              {busy && <Spinner size="sm" tone="inherit" />}{!busy && <Mail className="h-3 w-3" />}
              {mode === "change"
                ? t_i18n("Save and email {count, plural, one {# user} other {# users}}", { count: status.recipient_count })
                : t_i18n("Email {count, plural, one {# user} other {# users}}", { count: status.recipient_count })}
            </Button>
          </AiDisabledTip>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// ModelChangesDialog — review and email added / retired models
// ---------------------------------------------------------------------------
function providerName(provider: string, t_i18n: Translate): string {
  return provider ? (PROVIDER_DISPLAY_NAMES[provider] ?? provider) : t_i18n("Other");
}

/** The models of one kind the email announces, grouped by provider, each removable. */
function ChosenModels({
  models,
  retired = false,
  flagged,
  providerLabel,
  onRemove,
}: {
  models: CatalogModelRef[];
  retired?: boolean;
  /** Chosen models drawn as a warning (see the note under the list). */
  flagged: ReadonlySet<string>;
  providerLabel: (provider: string) => string;
  onRemove: (id: string) => void;
}) {
  const { t_i18n } = useFormatter();
  if (models.length === 0) return <p className="content-compact text-default-secondary">{t_i18n("None")}</p>;
  return (
    <div className="space-y-1.5">
      {groupByProvider(models).map((group) => (
        <div key={group.provider || "-"} className="flex flex-wrap items-center gap-1.5">
          <span className="w-24 shrink-0 truncate content-caption font-medium uppercase tracking-wide text-default-secondary">
            {providerLabel(group.provider)}
          </span>
          {group.models.map((model) => (
            <Badge
              key={model.id}
              variant={flagged.has(model.id) ? "warning" : retired ? "secondary" : "outline"}
              className="h-6 max-w-full gap-1 pr-1 text-[11px] font-medium"
            >
              <span className={cn("max-w-[220px] truncate", retired && "line-through opacity-80")}>{model.id}</span>
              <button
                type="button"
                aria-label={t_i18n("Remove {name}", { name: model.id })}
                className="rounded-sm text-default-secondary transition-colors hover:text-default-primary"
                onClick={() => onRemove(model.id)}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      ))}
    </div>
  );
}

/** One kind of the announcement: its chosen models, a way to clear them, and what is wrong with them. */
function ChosenKind({
  kind,
  models,
  flagged,
  note,
  providerLabel,
  onRemove,
  onClear,
}: {
  kind: AnnouncedKind;
  models: CatalogModelRef[];
  flagged: CatalogModelRef[];
  note: string | null;
  providerLabel: (provider: string) => string;
  onRemove: (id: string) => void;
  onClear: () => void;
}) {
  const { t_i18n } = useFormatter();
  const flaggedIds = useMemo(() => new Set(flagged.map((m) => m.id)), [flagged]);
  const title = kind === "new" ? t_i18n("New models") : t_i18n("Retired models");
  return (
    <section className="space-y-2" aria-label={title}>
      <div className="flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 content-compact font-medium">
          {kind === "new" ? <Plus className="h-3.5 w-3.5 text-accent" /> : <Minus className="h-3.5 w-3.5 text-default-secondary" />}
          {title}
          <span className="font-normal tabular-nums text-default-secondary">{models.length}</span>
        </p>
        {models.length > 0 && (
          <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px] text-default-secondary" onClick={onClear}>
            {t_i18n("Clear")}
          </Button>
        )}
      </div>
      <ChosenModels models={models} retired={kind === "retired"} flagged={flaggedIds} providerLabel={providerLabel} onRemove={onRemove} />
      {note && (
        <p role={kind === "new" ? "alert" : "note"} className="content-caption leading-relaxed text-feedback-warning-primary">
          {note}
        </p>
      )}
    </section>
  );
}

/** The email as the users would get it, rendered by the server from the current choice. */
function EmailPreview({ body, enabled }: { body: ModelAnnouncementEmailBody; enabled: boolean }) {
  const { t_i18n } = useFormatter();
  const debounced = useDebounce(body, 400);
  const preview = useModelAnnouncementPreview(debounced, enabled);
  if (!enabled) {
    return (
      <p className="flex h-[26rem] items-center justify-center px-6 text-center content-compact text-default-secondary">
        {t_i18n("Choose the models to announce to see the email.")}
      </p>
    );
  }
  if (!preview.data) {
    return preview.isError ? (
      <p role="alert" className="content-compact text-feedback-error-primary">
        {t_i18n("The preview could not be rendered: {error}", { error: preview.error?.message ?? "" })}
      </p>
    ) : (
      <div className="flex h-[26rem] items-center justify-center">
        <Spinner size="md" tone="inherit" className="text-default-secondary" label={t_i18n("Rendering the preview")} />
      </div>
    );
  }
  const updating = preview.isFetching || debounced !== body;
  return (
    <div className="space-y-2">
      <div className="flex items-start justify-between gap-2">
        <p className="min-w-0 content-compact leading-relaxed">
          <span className="text-default-secondary">{t_i18n("Subject")}</span>{" "}
          <span className="font-medium text-default-secondary">{preview.data.subject}</span>
        </p>
        {updating && <Spinner size="sm" tone="inherit" className="mt-0.5 shrink-0 text-default-secondary" label={t_i18n("Updating the preview")} />}
      </div>
      {/* No script, no navigation, no same-origin access: the frame only shows the email. */}
      <iframe
        title={t_i18n("Email preview")}
        sandbox=""
        srcDoc={preview.data.html}
        className={cn("h-[26rem] w-full rounded-sm border border-elevation-subtle-soft transition-opacity", updating && "opacity-70")}
      />
      {preview.isError && (
        <p role="alert" className="text-[11px] text-feedback-error-primary">
          {t_i18n("The preview could not be updated: {error}", { error: preview.error?.message ?? "" })}
        </p>
      )}
    </div>
  );
}

/**
 * The email of the models the users can now use and the ones retired, as the
 * admin edits it: `change` the saves not emailed yet (the editor starts from
 * them and emailing consumes them), `null` an announcement of their own.
 * `afterSave`: opened right after the save that produced `change`.
 */
export function ModelChangesDialog({
  change,
  status,
  afterSave = false,
  onClose,
}: {
  change: ModelCatalogChange | null;
  status: ModelAnnouncementStatus;
  afterSave?: boolean;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const announce = useAnnounceModelChanges();
  const emailingToast = useEmailingToast();
  const blocker = emailBlocker(status, t_i18n);
  const saved = useMemo(() => selectionOf(change), [change]);
  const [selection, setSelection] = useState<ModelSelection>(saved);
  const [pane, setPane] = useState<Pane>("selection");
  const busy = announce.isPending;
  const catalogs = useModelAnnouncementCatalogs(!aiOff);
  const providerLabel = useCallback((provider: string) => providerName(provider, t_i18n), [t_i18n]);

  const directory = useMemo(
    () =>
      buildDirectory({
        offered: status.offered_models,
        formerly: status.formerly_offered_models,
        listed: catalogs.data?.models ?? [],
        // A model taken out of the email stays in the list, to be put back.
        chosen: [...selection.added, ...selection.removed, ...saved.added, ...saved.removed],
      }),
    [status.offered_models, status.formerly_offered_models, catalogs.data, selection, saved],
  );
  const catalogsState: ProviderCatalogsState = {
    providers: aiOff ? null : (catalogs.data?.providers ?? null),
    loading: catalogs.isPending && catalogs.fetchStatus === "fetching",
    refreshing: catalogs.isRefetching,
    error: catalogs.isError && !catalogs.data ? (catalogs.error?.message ?? "") : null,
    onRefresh: () => void catalogs.refetch(),
  };

  // A save made while the dialog is open can take a new model off the list the users pick from.
  const { noLongerOffered, stillOffered } = selectionWarnings(selection, status.offered_models);
  const empty = isEmptySelection(selection);
  const tooMany = exceedsLimit(selection);
  const invalid = empty || tooMany || noLongerOffered.length > 0;
  const edited = !!change && !sameSelection(selection, saved);
  const body = useMemo(() => selectionBody(selection), [selection]);

  // Escape clears a search holding text before it closes the dialog.
  const keepOpenOnEscape = (event: KeyboardEvent) => {
    const target = event.target;
    if (target instanceof HTMLInputElement && target.hasAttribute("data-clear-on-escape") && target.value) event.preventDefault();
  };

  const emailUsers = () => {
    announce.mutate(
      { draft_id: change?.id ?? null, ...body },
      {
        onSuccess: () => {
          emailingToast(status);
          onClose();
        },
      },
    );
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="max-h-[92vh] max-w-5xl" onEscapeKeyDown={keepOpenOnEscape}>
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/10">
              <Mail className="h-5 w-5 text-accent" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">
                {change ? t_i18n("Email the users about these model changes?") : t_i18n("Announce model changes")}
              </DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {!change
                  ? t_i18n("Choose what the email announces: any model the users can pick as new, any model at all as retired.")
                  : afterSave
                    ? t_i18n("Your changes are saved. You can also email your users the models they can now use and the ones that were retired.")
                    : t_i18n("Review the model changes you saved before emailing them to your users.")}
              </DialogDescription>
              {change && (
                <p className="content-compact leading-relaxed text-default-secondary">
                  {t_i18n("{count, plural, one {# save} other {# saves}} since {date}, combined over the last {hours, plural, one {# hour} other {# hours}}. Edit what the email announces before sending it.", {
                    count: change.saves,
                    date: formatShortDateTime(change.first_saved_at),
                    hours: status.changes_window_hours,
                  })}
                </p>
              )}
            </div>
          </div>
        </DialogHeader>

        <fieldset
          disabled={busy}
          className="m-0 grid min-w-0 gap-4 border-0 p-0 pt-1 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]"
        >
          <ModelDirectory
            directory={directory}
            selection={selection}
            onChange={setSelection}
            providerLabel={providerLabel}
            catalogs={catalogsState}
          />
          <div className="min-w-0 space-y-3 rounded-sm bg-elevation-hover p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <SegmentedFilter
                ariaLabel={t_i18n("The email")}
                value={pane}
                onChange={setPane}
                options={[
                  { key: "selection", label: "In the email", count: selection.added.length + selection.removed.length },
                  { key: "preview", label: "Preview" },
                ]}
              />
              {edited && (
                <Button variant="ghost" size="sm" className="h-7 gap-1.5 px-2 content-compact" onClick={() => setSelection(saved)}>
                  <RotateCcw className="h-3 w-3" /> {t_i18n("Reset to the saved changes")}
                </Button>
              )}
            </div>
            {pane === "selection" ? (
              <div className="space-y-4">
                <ChosenKind
                  kind="new"
                  models={selection.added}
                  flagged={noLongerOffered}
                  note={
                    noLongerOffered.length > 0
                      ? t_i18n("The users can no longer pick {models}: remove them before emailing.", { models: modelIds(noLongerOffered) })
                      : null
                  }
                  providerLabel={providerLabel}
                  onRemove={(id) => setSelection((s) => ({ ...s, added: withoutModel(s.added, id) }))}
                  onClear={() => setSelection((s) => ({ ...s, added: [] }))}
                />
                <ChosenKind
                  kind="retired"
                  models={selection.removed}
                  flagged={stillOffered}
                  note={
                    stillOffered.length > 0
                      ? t_i18n("The users can still pick {models}: the email says they are retired all the same.", { models: modelIds(stillOffered) })
                      : null
                  }
                  providerLabel={providerLabel}
                  onRemove={(id) => setSelection((s) => ({ ...s, removed: withoutModel(s.removed, id) }))}
                  onClear={() => setSelection((s) => ({ ...s, removed: [] }))}
                />
              </div>
            ) : (
              <EmailPreview body={body} enabled={!invalid} />
            )}
          </div>
        </fieldset>

        <div className="space-y-3">
          <RecipientsNote status={status} />
          {canEmailUsers(status) && <TestEmailControl body={body} busy={busy} disabled={invalid} />}
          {announce.isError && (
            <p role="alert" className="content-base text-feedback-error-primary">
              {t_i18n("The users were not emailed: {error}", { error: announce.error?.message ?? "" })}
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 pt-2">
          <p className="content-compact text-default-secondary">
            {empty
              ? t_i18n("Choose at least one new or retired model to announce.")
              : tooMany
                ? t_i18n("At most {max} models of each kind can be announced at once.", { max: MAX_ANNOUNCED_MODELS })
                : t_i18n("{added, plural, one {# new model} other {# new models}} and {removed, plural, one {# retired model} other {# retired models}} in the email.", {
                    added: selection.added.length,
                    removed: selection.removed.length,
                  })}
          </p>
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
              {afterSave ? t_i18n("Don't email") : t_i18n("Cancel")}
            </Button>
            <AiDisabledTip>
              <Button size="sm" className="gap-1.5" disabled={aiOff || busy || !!blocker || invalid} onClick={emailUsers} aria-busy={busy || undefined}>
                {busy && <Spinner size="sm" tone="inherit" />}{!busy && <Mail className="h-3 w-3" />}
                {t_i18n("Email {count, plural, one {# user} other {# users}}", { count: status.recipient_count })}
              </Button>
            </AiDisabledTip>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// ModelAnnouncementPanel — the latest announcement and "Email users"
// ---------------------------------------------------------------------------
export function ModelAnnouncementPanel({ labelOf }: { labelOf: (model: string, provider: string | null) => string }) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const { data: status } = useModelAnnouncementStatus();
  const [open, setOpen] = useState<"default" | null>(null);
  // The draft as it was when the admin opened it: a save landing meanwhile stays for the next email.
  const [reviewing, setReviewing] = useState<{ change: ModelCatalogChange | null } | null>(null);
  if (!status) return null;

  const latest = status.latest;
  const unannounced = status.unannounced_change;
  const inFlight = !!latest && IN_FLIGHT.includes(latest.status);
  const inFlightForDefault = inFlight && latest?.default_model === status.default_model;
  const blocker = emailBlocker(status, t_i18n);
  const canEmail = !!status.default_model && !blocker && !inFlightForDefault;

  return (
    <div className="space-y-3 rounded-sm bg-elevation-default p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            {inFlight && <Spinner size="sm" tone="inherit" className="text-filigran-tonic-primary" />}{!inFlight && <Mail className="h-4 w-4 text-filigran-tonic-primary" />}
          </div>
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <p className="content-base font-medium">{t_i18n("User announcements")}</p>
              {latest && (
                <Badge variant="outline" className="px-1.5 py-0 content-caption font-normal text-default-secondary">
                  {latest.source === "env" ? t_i18n("At deploy") : t_i18n("From Settings")}
                </Badge>
              )}
            </div>
            <p className="text-[11px] leading-relaxed text-default-secondary">
              {latest
                ? outcomeLine(latest, status.emails_per_minute, t_i18n)
                : blocker ?? t_i18n("No announcement has been emailed yet. Email the users about the default model at any time.")}
            </p>
            {latest && inFlight && <DeliveryProgress latest={latest} />}
            {latest && <LastError latest={latest} />}
            {latest && <AnnouncedModels latest={latest} />}
            {latest && blocker && <p className="content-caption leading-relaxed text-feedback-warning-primary">{blocker}</p>}
          </div>
        </div>
        {canEmailUsers(status) && status.default_model && (
          <AiDisabledTip wrapClassName="shrink-0">
            <Button variant="secondary" size="sm" className="gap-1.5" disabled={aiOff || !canEmail} onClick={() => setOpen("default")}>
              <Send className="h-3 w-3" /> {t_i18n("Email users")}
            </Button>
          </AiDisabledTip>
        )}
      </div>
      {unannounced ? (
        <div className="flex items-start justify-between gap-3 border-t border-elevation-subtle-soft pl-11 pt-3">
          <div className="min-w-0 space-y-1">
            <p className="text-[11px] leading-relaxed text-default-secondary">
              {unannounced.saves > 1
                ? t_i18n("Model changes from {count, plural, one {# save} other {# saves}} since {date}, not emailed yet.", {
                    count: unannounced.saves,
                    date: formatShortDateTime(unannounced.first_saved_at),
                  })
                : t_i18n("Model changes saved on {date}, not emailed yet.", { date: formatShortDateTime(unannounced.created_at) })}
            </p>
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-default-secondary">
              <ModelChanges added={unannounced.added_models} removed={unannounced.removed_models} />
            </div>
          </div>
          {canEmailUsers(status) && (
            <AiDisabledTip wrapClassName="shrink-0">
              <Button variant="secondary" size="sm" className="gap-1.5" disabled={aiOff || !!blocker} onClick={() => setReviewing({ change: unannounced })}>
                <Send className="h-3 w-3" /> {t_i18n("Review and email")}
              </Button>
            </AiDisabledTip>
          )}
        </div>
      ) : (
        canEmailUsers(status) && (
          <div className="flex items-center justify-between gap-3 border-t border-elevation-subtle-soft pl-11 pt-3">
            <p className="min-w-0 text-[11px] leading-relaxed text-default-secondary">
              {t_i18n("No model change is waiting to be emailed. You can still announce new or retired models of your choice.")}
            </p>
            <AiDisabledTip wrapClassName="shrink-0">
              <Button variant="ghost" size="sm" className="gap-1.5" disabled={aiOff || !!blocker} onClick={() => setReviewing({ change: null })}>
                <Send className="h-3 w-3" /> {t_i18n("Announce model changes")}
              </Button>
            </AiDisabledTip>
          </div>
        )
      )}
      {open === "default" && status.default_model && (
        <DefaultModelAnnouncementDialog
          mode="announce"
          model={status.default_model}
          modelLabel={labelOf(status.default_model, status.default_provider)}
          provider={status.default_provider}
          previousModel={status.previous_default_model}
          status={status}
          onClose={() => setOpen(null)}
        />
      )}
      {reviewing && (
        <ModelChangesDialog change={reviewing.change} status={status} onClose={() => setReviewing(null)} />
      )}
    </div>
  );
}
