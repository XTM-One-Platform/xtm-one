// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Usage and quotas > Storage & retention (#396): how long each
 * category of stored file is kept, what it holds today, the next sweep and
 * the last one. Administrators change the days; a value shorter than the
 * stored one needs a Preview (the dry run of the next sweep) before Save. A
 * category the environment sets is shown locked. 0 keeps the files for good
 * ("Never"). A deleted file keeps a card in the chat; only its bytes go.
 */
import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { CompactFigure } from "@/components/ui/compact-figure";
import { Input } from "@/components/ui/input";
import { InfoTip } from "@/components/ui/info-tip";
import { SettingsCard, SettingsSaveBar } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import {
  type FileRetentionCategory,
  type FileRetentionPreview,
  useFileRetention,
  usePreviewFileRetention,
  useSaveFileRetention,
} from "@/hooks/api/useFileRetentionApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { formatFileSize } from "@/pages/chat/fileVisuals";

/**
 * The English name, what each category covers and its info bubble: the unit,
 * a real example, 0 = Never, the basis, what the sweep does. @i18n-keys
 */
const CATEGORY_LABELS = {
  agent_working: {
    name: "Agent working files",
    covers: "Scratch files agents write while they work",
    help: "Days after the file's last use (a download, a preview, an agent reading it). Example: 60 deletes a scratch CSV no one opened for 60 days. 0 = Never. The hourly sweep deletes the content and keeps a deleted card in the conversation.",
  },
  agent_download: {
    name: "Agent deliverables",
    covers: "Files agents hand to people to download",
    help: "Days after the file's last use (a download, a preview, an agent reading it). Example: 60 deletes a report PDF no one downloaded for 60 days. 0 = Never. The hourly sweep deletes the content and keeps a deleted card in the conversation.",
  },
  agent: {
    name: "Other agent files",
    covers: "Files tools and MCP servers return",
    help: "Days after the file was created. Example: 90 deletes an MCP export 90 days after it arrived. 0 = Never. The hourly sweep deletes the content and keeps a deleted card.",
  },
  chat: {
    name: "Chat uploads",
    covers: "Files people attach in a conversation",
    help: "Days after the upload. Example: 365 deletes a spreadsheet a year after someone attached it. 0 = Never. The hourly sweep deletes the content and keeps a deleted card in the conversation.",
  },
  channel: {
    name: "Channel files",
    covers: "Files received from Teams, Outlook and other channels",
    help: "Days after the file arrived. Example: 180 deletes a Teams attachment six months after it was received. 0 = Never. The hourly sweep deletes the content and keeps a deleted card.",
  },
  agent_check: {
    name: "Check pictures",
    covers: "Frames and captures a tool makes for the agent to look at",
    help: "Days after the capture. Example: 7 deletes a screenshot an agent checked a week ago. 0 = Never. The hourly sweep deletes the content and keeps a deleted card.",
  },
} as const;

type CategoryLabel = { name: string; covers: string; help: string };

function categoryLabel(key: string): CategoryLabel | undefined {
  return (CATEGORY_LABELS as Record<string, CategoryLabel>)[key];
}

function parseDays(raw: string, max: number): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const value = Number.parseInt(raw, 10);
  return value >= 0 && value <= max ? value : null;
}

const StorageGlyph = ({ className }: { className?: string }) => <Icon name="database" size={16} className={className} />;

export function FileRetentionSection({ isAdmin }: { isAdmin: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const numbers = useNumberFormat();
  const fieldId = useId();
  const { toast } = useToast();
  const { data, isLoading, isError } = useFileRetention(isAdmin);
  const save = useSaveFileRetention();
  const preview = usePreviewFileRetention();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [previewed, setPreviewed] = useState<{ key: string; result: FileRetentionPreview } | null>(null);

  const categories = data?.categories ?? [];
  const maxDays = data?.max_days ?? 3650;
  const editable = categories.filter((c) => !c.locked);
  const parsed: Record<string, number> = {};
  for (const c of editable) {
    const value = parseDays(draft[c.category] ?? "", maxDays);
    if (value !== null && value !== c.days) parsed[c.category] = value;
  }
  const invalid = editable.some((c) => editing && parseDays(draft[c.category] ?? "", maxDays) === null);
  // A shorter value (or Never lifted) deletes more at the next sweep: Preview first.
  const shortens = Object.entries(parsed).some(([key, days]) => {
    const current = categories.find((c) => c.category === key);
    return !!current && days > 0 && (current.days === 0 || days < current.days);
  });
  const draftKey = JSON.stringify(parsed);
  const previewFresh = previewed?.key === draftKey;

  const days = (count: number) => (count > 0 ? t_i18n("{count, plural, one {# day} other {# days}}", { count }) : t_i18n("Never"));
  const when = (iso: string) => intl.formatDate(new Date(iso), { dateStyle: "medium", timeStyle: "short" });
  const fileCount = (count: number) => t_i18n("{count, plural, one {# file} other {# files}}", { count });

  const startEditing = () => {
    setDraft(Object.fromEntries(categories.map((c) => [c.category, String(c.days)])));
    setPreviewed(null);
    setEditing(true);
  };
  const runPreview = () =>
    preview.mutate(parsed, {
      onSuccess: (result) => setPreviewed({ key: draftKey, result }),
      onError: (err) => toast({ title: t_i18n("The preview failed"), description: err instanceof Error ? err.message : undefined, variant: "error" }),
    });
  const submit = () =>
    save.mutate(parsed, {
      onSuccess: () => {
        setEditing(false);
        setPreviewed(null);
        toast({ title: t_i18n("File retention saved"), variant: "success" });
      },
      onError: (err) =>
        toast({ title: t_i18n("The file retention was not saved"), description: err instanceof Error ? err.message : undefined, variant: "error" }),
    });

  if (!isAdmin) {
    return (
      <p className="content-base text-default-secondary" data-testid="settings.file-retention.admin-only">
        {t_i18n("Only administrators see and change the file retention.")}
      </p>
    );
  }

  const basis = (c: FileRetentionCategory) =>
    c.never ? t_i18n("Kept for good") : c.basis === "last_use" ? t_i18n("after the last use") : t_i18n("after creation");
  const previewRow = (key: string) => previewed?.result.categories.find((p) => p.category === key);

  return (
    <SettingsCard
      data-testid="settings.file-retention"
      icon={StorageGlyph}
      title={
        <span className="flex items-center gap-1.5">
          {t_i18n("Storage & retention")}
          <InfoTip
            label={t_i18n("How the file retention works")}
            content={t_i18n(
              "A sweep runs every hour. A file a work item, workset, shared state, presentation, report or video still uses is kept. A deleted file stays in the conversation as a card: only its content goes.",
            )}
          />
        </span>
      }
      description={t_i18n("How long each category of stored file is kept.")}
      action={
        !editing && (
          <Button variant="secondary" size="sm" className="gap-1.5" onClick={startEditing} disabled={isLoading || !data}>
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
          </Button>
        )
      }
    >
      <div className="space-y-4">
      {isError && <p className="content-base text-feedback-error-primary">{t_i18n("The file retention could not be read.")}</p>}

      <table className="w-full content-base" data-testid="settings.file-retention.table">
        <thead>
          <tr className="border-b border-elevation-subtle-soft text-left text-default-secondary">
            <th className="py-2 pr-3 font-medium">{t_i18n("Category")}</th>
            <th className="py-2 pr-3 font-medium">{t_i18n("Kept")}</th>
            <th className="py-2 pr-3 text-right font-medium">{t_i18n("Files")}</th>
            <th className="py-2 pr-3 text-right font-medium">{t_i18n("Size")}</th>
            <th className="py-2 text-right font-medium">{t_i18n("Last sweep")}</th>
          </tr>
        </thead>
        <tbody>
          {categories.map((c) => {
            const label = categoryLabel(c.category);
            const inputId = `${fieldId}-${c.category}`;
            const fieldInvalid = editing && !c.locked && parseDays(draft[c.category] ?? "", maxDays) === null;
            const swept = data?.last_sweep?.files[c.category];
            const would = previewRow(c.category);
            return (
              <tr key={c.category} className="border-b border-elevation-subtle-soft align-top" data-testid={`settings.file-retention.row.${c.category}`}>
                <td className="py-2 pr-3">
                  <span className="flex items-center gap-1.5">
                    <label htmlFor={inputId} className="font-medium">
                      {t_i18n(label?.name ?? c.category)}
                    </label>
                    {label && (
                      <InfoTip
                        label={t_i18n("About {name}", { name: t_i18n(label.name) })}
                        content={t_i18n(label.help)}
                      />
                    )}
                  </span>
                  <div className="content-compact text-default-secondary">{t_i18n(label?.covers ?? "")}</div>
                </td>
                <td className="py-2 pr-3">
                  {editing && !c.locked ? (
                    <span className="flex items-center gap-2">
                      <Input
                        id={inputId}
                        aria-label={t_i18n(label?.name ?? c.category)}
                        type="number"
                        min={0}
                        max={maxDays}
                        className="w-24"
                        aria-invalid={fieldInvalid || undefined}
                        value={draft[c.category] ?? ""}
                        onChange={(e) => setDraft({ ...draft, [c.category]: e.target.value })}
                        disabled={save.isPending}
                      />
                      <span className="text-default-secondary">{t_i18n("days (0 = never)")}</span>
                    </span>
                  ) : (
                    <span className="flex items-center gap-1.5">
                      <span className="font-medium tabular-nums">{days(c.days)}</span>
                      {!c.never && <span className="text-default-secondary">{basis(c)}</span>}
                      {c.locked && (
                        <span className="flex items-center gap-1 text-default-secondary" title={t_i18n("Set by {name}", { name: c.env_var })}>
                          <Icon name="lock" size={16} aria-hidden /> {t_i18n("Set by {name}", { name: c.env_var })}
                        </span>
                      )}
                    </span>
                  )}
                  {fieldInvalid && (
                    <p role="alert" className="content-compact text-feedback-error-primary">
                      {t_i18n("Enter a whole number from 0 to 3650")}
                    </p>
                  )}
                  {editing && would && (would.files > 0 || would.kept_referenced > 0) && (
                    <p className="content-compact text-default-secondary" data-testid={`settings.file-retention.preview.${c.category}`}>
                      {t_i18n("Next sweep: {files} ({size}) deleted; {kept, plural, one {# file} other {# files}} kept in use", {
                        files: fileCount(would.files),
                        size: formatFileSize(would.bytes) || "0 B",
                        kept: would.kept_referenced,
                      })}
                    </p>
                  )}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  <CompactFigure exact={numbers.exactCount(c.files)} testId={`settings.file-retention.files.${c.category}`}>
                    {numbers.count(c.files)}
                  </CompactFigure>
                  {c.deleted > 0 && (
                    <div className="content-compact text-default-secondary">
                      <CompactFigure exact={t_i18n("{count, plural, one {# deleted} other {# deleted}}", { count: c.deleted })}>
                        {t_i18n("{count, plural, one {{shown} deleted} other {{shown} deleted}}", { count: c.deleted, shown: numbers.count(c.deleted) })}
                      </CompactFigure>
                    </div>
                  )}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">{formatFileSize(c.bytes) || "0 B"}</td>
                <td className="py-2 text-right tabular-nums text-default-secondary">
                  {swept != null ? (
                    <CompactFigure exact={fileCount(swept)} testId={`settings.file-retention.swept.${c.category}`}>
                      {t_i18n("{count, plural, one {{shown} file} other {{shown} files}}", { count: swept, shown: numbers.count(swept) })}
                    </CompactFigure>
                  ) : (
                    "-"
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {data && (
        <p className="content-compact text-default-secondary" data-testid="settings.file-retention.schedule">
          {data.next_sweep_at ? t_i18n("Next sweep {next}.", { next: when(data.next_sweep_at) }) : t_i18n("Next sweep: due now, at the next scheduler tick.")}{" "}
          {data.last_sweep ? t_i18n("Last sweep {last}.", { last: when(data.last_sweep.at) }) : t_i18n("No sweep has run yet.")}
        </p>
      )}
      {data?.sweep_overdue && (
        <p className="content-compact flex items-center gap-1.5" role="status" data-testid="settings.file-retention.overdue">
          <Icon name="triangle-alert" size={16} className="shrink-0 text-feedback-warning-primary" aria-hidden />
          {data.maintenance_completed_at
            ? t_i18n("The hourly sweep has not completed since {last}: files past their retention are not deleted.", { last: when(data.maintenance_completed_at) })
            : t_i18n("The hourly sweep has not completed yet: files past their retention are not deleted.")}
        </p>
      )}

      {editing && (
        <>
          {shortens && !previewFresh && (
            <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-default p-3">
              <p className="content-base">{t_i18n("A shorter retention deletes files at the next sweep. Preview what goes before saving.")}</p>
              <Button variant="secondary" size="sm" className="shrink-0 gap-1.5" onClick={runPreview} disabled={invalid || preview.isPending}>
                <Icon name="eye" size={16} aria-hidden /> {t_i18n("Preview")}
              </Button>
            </div>
          )}
          {previewFresh && previewed && (
            <p className="content-base" data-testid="settings.file-retention.preview-total" role="status">
              {t_i18n("The next sweep would delete {files} ({size}).", {
                files: fileCount(previewed.result.total_files),
                size: formatFileSize(previewed.result.total_bytes) || "0 B",
              })}
            </p>
          )}
          <SettingsSaveBar
            dirty={Object.keys(parsed).length > 0}
            saving={save.isPending}
            saveDisabled={invalid || (shortens && !previewFresh)}
            onSave={submit}
            onCancel={() => {
              setEditing(false);
              setPreviewed(null);
            }}
          />
        </>
      )}
      </div>
    </SettingsCard>
  );
}
