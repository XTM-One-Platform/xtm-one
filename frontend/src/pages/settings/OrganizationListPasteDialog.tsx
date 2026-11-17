// SPDX-License-Identifier: Apache-2.0

/**
 * "Paste a list" of an organization profile list card in edit (#12256, slice 2).
 *
 * The person pastes lines (one row per line, "Jira, Atlassian", a tab, a semicolon or " - "
 * between the fields) or chooses the CSV the card's Export wrote; the preview says per line
 * what is added, what is already in the list and what cannot be read, and Add puts the new
 * rows in the edit, pinned first like a row added by hand. Nothing is saved: the card's Save
 * bar writes them with the rest of the edit.
 */
import { useId, useMemo, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { type PasteSpec, parsePaste, pastedValues } from "@/lib/organization-list-paste";

/** The lines the preview lists before it says how many more there are. */
const PREVIEW_LINES = 50;

export function OrganizationListPasteDialog<V>({
  open,
  onOpenChange,
  title,
  example,
  spec,
  existing,
  rowLabel,
  fieldLabels,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The dialog's title, translated. */
  title: string;
  /** One example line, translated ("Jira, Atlassian"). */
  example: string;
  spec: PasteSpec<V>;
  /** The rows the list holds now: a pasted line already there is not added again. */
  existing: readonly V[];
  rowLabel: (value: V) => string;
  /** The column header of each field, translated, for the line errors ("Unknown value in Kind"). */
  fieldLabels: Record<string, string>;
  onAdd: (values: V[]) => void;
}) {
  const { t_i18n } = useFormatter();
  const textId = useId();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [text, setText] = useState("");
  const [fileError, setFileError] = useState<string | null>(null);
  const lines = useMemo(() => parsePaste(text, spec, existing), [text, spec, existing]);
  const values = pastedValues(lines);
  const duplicates = lines.filter((line) => line.status === "duplicate").length;
  const errors = lines.filter((line) => line.status === "error");

  const close = () => {
    setText("");
    setFileError(null);
    onOpenChange(false);
  };
  const readFile = async (file: File) => {
    setFileError(null);
    try {
      setText(await file.text());
    } catch {
      setFileError(t_i18n("The file could not be read."));
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <DialogContent size="md" data-testid="settings.organization.paste-dialog">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {t_i18n("One row per line, the fields in the order of the columns, separated by a comma, a tab, a semicolon or a dash. Example: {example}", { example })}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <SettingsField
            id={textId}
            label={t_i18n("Lines to add")}
            helper={t_i18n("A CSV exported from this list works too: its first line names the columns.")}
            error={fileError}
          >
            <textarea
              id={textId}
              value={text}
              onChange={(event) => setText(event.target.value)}
              aria-describedby={settingsFieldHintId(textId)}
              placeholder={example}
              data-testid="settings.organization.paste-text"
              className={`h-40 w-full resize-y rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
            />
          </SettingsField>
          <div className="flex items-center gap-2">
            <Button variant="secondary" className="gap-1.5" onClick={() => fileRef.current?.click()}>
              <Icon name="upload" size={16} aria-hidden /> {t_i18n("Choose a CSV file")}
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv,text/plain"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void readFile(file);
                event.target.value = "";
              }}
            />
          </div>
          {lines.length > 0 && (
            <div className="space-y-2" data-testid="settings.organization.paste-preview">
              <p className="content-base text-default-primary" role="status">
                {[
                  t_i18n("{count, plural, one {# row to add} other {# rows to add}}", { count: values.length }),
                  duplicates ? t_i18n("{count, plural, one {# already in the list} other {# already in the list}}", { count: duplicates }) : "",
                  errors.length ? t_i18n("{count, plural, one {# line not read} other {# lines not read}}", { count: errors.length }) : "",
                ]
                  .filter(Boolean)
                  .join(", ")}
              </p>
              <ul className="max-h-48 space-y-0.5 overflow-y-auto content-compact">
                {lines.slice(0, PREVIEW_LINES).map((line) => (
                  <li
                    key={line.line}
                    data-status={line.status}
                    className={line.status === "error" ? "text-feedback-error-primary" : line.status === "duplicate" ? "text-default-secondary" : "text-default-primary"}
                  >
                    {line.status === "new" && line.value
                      ? t_i18n("Line {line}: {name}", { line: line.line, name: rowLabel(line.value) })
                      : line.status === "duplicate" && line.value
                        ? t_i18n("Line {line}: {name} is already in the list", { line: line.line, name: rowLabel(line.value) })
                        : line.field
                          ? t_i18n("Line {line}: {error} in {field}", { line: line.line, error: t_i18n(line.error ?? ""), field: fieldLabels[line.field] ?? line.field })
                          : t_i18n("Line {line}: {error}", { line: line.line, error: t_i18n(line.error ?? "") })}
                  </li>
                ))}
              </ul>
              {lines.length > PREVIEW_LINES && (
                <p className="content-compact text-default-secondary">{t_i18n("{count, plural, one {# more line} other {# more lines}}", { count: lines.length - PREVIEW_LINES })}</p>
              )}
            </div>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={close}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            className="gap-1.5"
            disabled={values.length === 0}
            data-testid="settings.organization.paste-add"
            onClick={() => {
              onAdd(values);
              close();
            }}
          >
            <Icon name="plus" size={16} aria-hidden />
            {t_i18n("{count, plural, =0 {Add} one {Add # row} other {Add # rows}}", { count: values.length })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
