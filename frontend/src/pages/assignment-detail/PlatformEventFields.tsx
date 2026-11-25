/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { useId } from "react";
import { Icon } from "@filigran/design-system";
import { Clock, Radio } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { usePlatformEvents } from "@/hooks/api/useAssignmentApi";
import { useAuth } from "@/hooks/useAuth";
import {
  platformDeadlineSavable,
  platformDeadlineState,
  platformEventDraft,
  platformEventFamily,
  platformWatchState,
  platformEventScope,
  type PlatformEventDraft,
  type PlatformFilterRow,
} from "@/lib/platform-events";
import { isoToWallTime, viewerTimeZone, wallTimeToOffsetIso } from "@/lib/scheduled-work-items";
import { useFormatter } from "@/i18n";
import type { TriggerConfig } from "@/types";

export function PlatformEventFields({
  draft, onChange, isAdmin,
}: { draft: PlatformEventDraft; onChange: (next: PlatformEventDraft) => void; isAdmin: boolean }) {
  const { t_i18n } = useFormatter();
  const { data: events, isLoading } = usePlatformEvents();
  const selected = draft.events[0] ?? "";
  const spec = events?.find((e) => e.event === selected);
  const ids = useId();
  const eventId = `${ids}-event`;
  const filtersId = `${ids}-filters`;
  const scopeId = `${ids}-scope`;
  const setRow = (index: number, patch: Partial<PlatformFilterRow>) =>
    onChange({ ...draft, rows: draft.rows.map((r, i) => (i === index ? { ...r, ...patch } : r)) });

  return (
    <div className="space-y-4 rounded-sm bg-elevation-hover p-3">
      <div className="space-y-1.5">
        <Label htmlFor={eventId} className="block text-default-secondary">{t_i18n("Event Type")}</Label>
        <SearchableSelect
          id={eventId}
          aria-label={t_i18n("Event Type")}
          options={(events ?? []).map((e) => ({ value: e.event, label: e.event, description: e.description, group: platformEventFamily(e.event), keywords: [e.description] }))}
          value={selected}
          onValueChange={(v) => onChange({ ...draft, events: v ? [v] : [] })}
          placeholder={isLoading ? t_i18n("Loading...") : t_i18n("Select event...")}
        />
        {draft.events.length > 1 && (
          <p className="content-caption text-default-secondary">{t_i18n("Also subscribed to: {events}", { events: draft.events.slice(1).join(", ") })}</p>
        )}
        {spec && <p className="content-caption text-default-secondary">{spec.description}</p>}
      </div>

      <div className="space-y-1.5" role="group" aria-labelledby={filtersId}>
        <Label id={filtersId} className="block text-default-secondary">
          {t_i18n("Filters")} <span className="text-default-secondary">{t_i18n("(optional)")}</span>
        </Label>
        {draft.rows.map((row, i) => (
          <div key={i} className="flex items-center gap-2">
            {spec ? (
              <SearchableSelect
                aria-label={t_i18n("Field")}
                className="w-56 shrink-0"
                searchable="auto"
                options={spec.filter_keys.map((k) => ({ value: k, label: k }))}
                value={row.key}
                onValueChange={(v) => setRow(i, { key: v })}
                placeholder={t_i18n("Field")}
              />
            ) : (
              <Input aria-label={t_i18n("Field")} className="w-56 shrink-0 [&_input]:font-mono-plex" placeholder={t_i18n("Field")} value={row.key} onChange={(e) => setRow(i, { key: e.target.value })} />
            )}
            <Input aria-label={t_i18n("Value")} className="[&_input]:font-mono-plex" placeholder={t_i18n("Value")} value={row.value} onChange={(e) => setRow(i, { value: e.target.value })} />
            <Button variant="ghost" size="icon" className="shrink-0" aria-label={t_i18n("Remove filter")} onClick={() => onChange({ ...draft, rows: draft.rows.filter((_, j) => j !== i) })}>
              <Icon name="trash-2" size={16} aria-hidden />
            </Button>
          </div>
        ))}
        <Button variant="outline" className="gap-1.5" onClick={() => onChange({ ...draft, rows: [...draft.rows, { key: "", value: "" }] })}>
          <Icon name="plus" size={16} aria-hidden /> {t_i18n("Add filter")}
        </Button>
        <p className="content-caption text-default-secondary">{t_i18n("Each filter names a field of the event and the value it must have. Without a filter, every event of this kind in scope starts a run.")}</p>
      </div>

      {isAdmin && (
        <div className="space-y-1.5">
          <Label htmlFor={scopeId} className="block text-default-secondary">{t_i18n("Scope")}</Label>
          <Select value={draft.scope} onValueChange={(v) => onChange({ ...draft, scope: platformEventScope(v) })}>
            <SelectTrigger id={scopeId} className="w-96 max-w-full" aria-label={t_i18n("Scope")}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="own">{t_i18n("This agent's objects")}</SelectItem>
              <SelectItem value="fleet">{t_i18n("Objects of this agent's fleet")}</SelectItem>
              <SelectItem value="all">{t_i18n("Every object")}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      )}

      <div className="flex items-start justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3">
        <div className="space-y-0.5">
          <p className="content-compact font-medium">{t_i18n("One-off watch")}</p>
          <p className="content-caption text-default-secondary leading-relaxed">
            {t_i18n("The first matching event starts one run, then the trigger stops. Save the trigger again to watch for the next one.")}
          </p>
        </div>
        <Switch aria-label={t_i18n("One-off watch")} checked={draft.once} onCheckedChange={(v) => onChange({ ...draft, once: v })} />
      </div>

      <div className="flex items-start justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3">
        <div className="space-y-0.5">
          <p className="content-compact font-medium">{t_i18n("Observer only")}</p>
          <p className="content-caption text-default-secondary leading-relaxed">
            {t_i18n("This automation watches the objects of its events without working them: a CTEM case it hears is not shown as worked by it.")}
          </p>
        </div>
        <Switch
          aria-label={t_i18n("Observer only")}
          checked={draft.observer === true}
          onCheckedChange={(v) => {
            const next = { ...draft };
            if (v) next.observer = true;
            else delete next.observer;
            onChange(next);
          }}
        />
      </div>

      <PlatformDeadlineField draft={draft} onChange={onChange} />
    </div>
  );
}

/** The optional deadline of a platform event trigger, a wall time in the viewer's zone sent with its offset. */
function PlatformDeadlineField({ draft, onChange }: { draft: PlatformEventDraft; onChange: (next: PlatformEventDraft) => void }) {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  const fieldId = "platform-event-deadline";
  const parses = !!draft.expiresAt && Number.isFinite(Date.parse(draft.expiresAt));
  const value = parses && draft.expiresAt ? isoToWallTime(draft.expiresAt, zone) : "";
  const passed = !platformDeadlineSavable(draft.expiresAt);
  const setValue = (wall: string) => {
    const iso = wall ? wallTimeToOffsetIso(wall, zone) : null;
    const next = { ...draft };
    if (iso) next.expiresAt = iso;
    else delete next.expiresAt;
    onChange(next);
  };
  return (
    <div className="space-y-1.5" data-testid="platform-event-deadline-field">
      <Label htmlFor={fieldId} className="block text-default-secondary">
        {t_i18n("Deadline")} <span className="text-default-secondary">{t_i18n("(optional)")}</span>
      </Label>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={fieldId}
          type="datetime-local"
          aria-label={t_i18n("Deadline ({zone})", { zone })}
          aria-invalid={passed || undefined}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="w-auto"
        />
        <span className="content-caption text-default-secondary">{zone}</span>
        {draft.expiresAt && (
          <Button variant="ghost" className="gap-1.5" onClick={() => setValue("")}>
            <Icon name="x" size={16} aria-hidden /> {t_i18n("Clear")}
          </Button>
        )}
      </div>
      {passed ? (
        <p className="content-caption text-feedback-error-primary" role="alert">
          {t_i18n("This deadline has passed. Pick a later time or clear it to save the trigger.")}
        </p>
      ) : (
        <p className="content-caption text-default-secondary">{t_i18n("Past this time, the trigger no longer starts runs. Without a deadline, it keeps listening.")}</p>
      )}
    </div>
  );
}

/** The read-only summary of a platform event trigger. */
export function PlatformEventSummary({ config }: { config: TriggerConfig }) {
  const { t_i18n, intl } = useFormatter();
  const { user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  const draft = platformEventDraft(config);
  const watch = platformWatchState(config);
  const deadline = platformDeadlineState(config.expires_at);
  // A deadline that does not parse is shown without a date, never as "Invalid Date".
  const when = (iso: string | undefined) =>
    iso && Number.isFinite(Date.parse(iso))
      ? intl.formatDate(iso, { dateStyle: "medium", timeStyle: "short", timeZone: zone })
      : null;
  const until = when(config.expires_at);
  const firedAt = when(config.fired_at);
  return (
    <div className="space-y-2 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4">
      <div className="flex items-center gap-2 mb-1">
        <Radio className="h-4 w-4 text-accent" />
        <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Platform event")}</p>
      </div>
      <div className="flex flex-wrap gap-2 content-caption">
        {draft.events.map((e) => (
          <code key={e} className="rounded-sm bg-elevation-hover px-2 py-0.5 text-accent">{e}</code>
        ))}
        {draft.rows.map((r) => (
          <span key={r.key} className="rounded-sm bg-elevation-hover px-2 py-0.5">
            <code>{r.key}</code> = <code className="text-accent">{r.value}</code>
          </span>
        ))}
        <span className="rounded-sm bg-elevation-hover px-2 py-0.5">
          {draft.scope === "all"
            ? t_i18n("Every object")
            : draft.scope === "fleet"
              ? t_i18n("Objects of this agent's fleet")
              : t_i18n("This agent's objects")}
        </span>
        {watch === "armed" && (
          <span className="rounded-sm bg-accent/10 px-2 py-0.5 text-accent">
            {until
              ? t_i18n("One-off watch: waiting for its event until {date}", { date: until })
              : t_i18n("One-off watch: waiting for its event")}
          </span>
        )}
        {watch === "expired" && (
          <span className="inline-flex items-center gap-1 rounded-sm bg-feedback-warning-primary/10 px-2 py-0.5 text-feedback-warning-tertiary">
            <Clock className="h-3 w-3" />
            {until ? t_i18n("One-off watch: expired on {date}, no event came", { date: until }) : t_i18n("One-off watch: expired, no event came")}
          </span>
        )}
        {watch === "fired" && (
          <span className="rounded-sm bg-elevation-hover px-2 py-0.5 text-default-secondary">
            {firedAt ? t_i18n("One-off watch: fired on {date}", { date: firedAt }) : t_i18n("One-off watch: fired")}
          </span>
        )}
        {watch === null && deadline === "active" && until && (
          <span className="rounded-sm bg-elevation-hover px-2 py-0.5">{t_i18n("Listening until {date}", { date: until })}</span>
        )}
        {watch === null && deadline === "ended" && (
          <span className="inline-flex items-center gap-1 rounded-sm bg-feedback-warning-primary/10 px-2 py-0.5 text-feedback-warning-tertiary">
            <Clock className="h-3 w-3" />
            {until ? t_i18n("Ended on {date}: no longer starts runs", { date: until }) : t_i18n("Ended: no longer starts runs")}
          </span>
        )}
      </div>
    </div>
  );
}
