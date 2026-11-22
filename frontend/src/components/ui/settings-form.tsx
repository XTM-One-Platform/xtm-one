// SPDX-License-Identifier: Apache-2.0

/**
 * The settings form kit (#9173, S1c): the anatomy every Settings section is
 * built from, taken from the agent page's Config and Overview tabs so a
 * setting reads like the rest of the product.
 *
 *   SettingsOverview  a row of StatCards: the section's state and key figures,
 *                     read-only (no model call, no run).
 *   SettingsCard      one group of settings: a CardTitle, a one-line
 *                     description and an optional action.
 *   SettingsFieldGrid one column when narrow, two when wide, never three tiny
 *                     ones.
 *   SettingsField     the label above with its info bubble (what it is, the
 *                     unit, an example, who reads it), the full-size field,
 *                     then the helper line or the inline error, left-aligned;
 *                     a required marker. The field passes
 *                     `aria-describedby={settingsFieldHintId(id)}`. A field
 *                     with an error is marked `aria-invalid` (the control
 *                     whose id is the field's, when it does not set it);
 *                     a refused Save calls `focusFirstInvalidField`.
 *   SettingsSwitchField
 *                     a setting that is a switch: the same label, info
 *                     bubble and helper as SettingsField, the switch at the
 *                     row's end, and the fields it reveals under it.
 *   SettingsSaveBar   Save and Cancel, sticky at the bottom of the scroll
 *                     area while the form holds unsaved changes.
 *   SettingsGroupTitle / SettingsGroupDescription
 *                     the heading and one-line description of a group inside
 *                     a section that keeps its own surface (a provider list, a
 *                     model role with its Configure action): the CardTitle
 *                     scale, never an uppercase micro-heading or 11 px text.
 *
 * A field in a settings form is never the `compact` Input: that size fits a
 * table cell, not a form (settings-form-kit.test.ts).
 */
import * as React from "react";
import { Icon, Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { InfoTip } from "@/components/ui/info-tip";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

export function SettingsOverview({ children, className, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("grid gap-4 sm:grid-cols-2 lg:grid-cols-4", className)} {...rest}>{children}</div>;
}

/** The props a page hands a collapsible card: its count line and its folded state. */
export interface SettingsCardFold {
  meta?: React.ReactNode;
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
}

interface SettingsCardProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  title: React.ReactNode;
  description?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  action?: React.ReactNode;
  /** What the title counts, drawn after it in secondary text ("152 · 4 need attention"). */
  meta?: React.ReactNode;
  /**
   * A collapsible card (#12256): the chevron before the title folds the body away, the header
   * (title, count, actions) stays. Controlled, so a jump to the card can open it again.
   */
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
}

export function SettingsCard({ title, description, icon: CardIcon, action, meta, collapsed = false, onCollapsedChange, children, className, ...rest }: SettingsCardProps) {
  const { t_i18n } = useFormatter();
  const bodyId = React.useId();
  const folded = Boolean(onCollapsedChange) && collapsed;
  // An action-only card (no body) draws no CardContent, so its padding stays balanced.
  const hasBody = React.Children.toArray(children).length > 0;
  return (
    <Card className={cn("min-w-0", className)} data-collapsed={onCollapsedChange ? String(folded) : undefined} {...rest}>
      <CardHeader className={cn("flex-row items-start justify-between gap-4 space-y-0 p-4", folded || !hasBody ? "pb-4" : "pb-3")}>
        <div className="min-w-0 space-y-1">
          <CardTitle className="flex items-center gap-2 content-base">
            {onCollapsedChange && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="-ml-1 h-6 w-6 shrink-0"
                aria-expanded={!folded}
                aria-controls={bodyId}
                aria-label={t_i18n(folded ? "Expand" : "Collapse")}
                data-testid="settings.card-collapse"
                onClick={() => onCollapsedChange(!folded)}
              >
                <Icon name={folded ? "chevron-right" : "chevron-down"} size={16} aria-hidden />
              </Button>
            )}
            {CardIcon && <CardIcon className="h-4 w-4 shrink-0 text-accent" />}
            {title}
            {meta && (
              <span className="content-compact font-normal text-default-secondary" data-testid="settings.card-count">
                {meta}
              </span>
            )}
          </CardTitle>
          {description && !folded && <CardDescription>{description}</CardDescription>}
        </div>
        {action && <div className="flex shrink-0 flex-wrap items-center gap-2">{action}</div>}
      </CardHeader>
      {!folded && hasBody && (
        <CardContent id={bodyId} className="px-4 pb-4 pt-0 content-base">
          {children}
        </CardContent>
      )}
    </Card>
  );
}

interface SettingsGroupTitleProps extends React.HTMLAttributes<HTMLHeadingElement> {
  icon?: React.ComponentType<{ className?: string }>;
  /** The icon colour, when the group carries a tone (a severity); the accent otherwise. */
  iconClassName?: string;
}

/** The heading of a settings group: the CardTitle scale with its accent (or tone) icon. */
export function SettingsGroupTitle({ icon: Icon, iconClassName, children, className, ...rest }: SettingsGroupTitleProps) {
  return (
    <h4 className={cn("flex items-center gap-2 content-base font-semibold text-default-primary", className)} {...rest}>
      {Icon && <Icon className={cn("h-4 w-4 shrink-0", iconClassName ?? "text-accent")} />}
      {children}
    </h4>
  );
}

/** The one-line description under a SettingsGroupTitle. */
export function SettingsGroupDescription({ children, className, ...rest }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("mt-1 content-compact text-default-secondary", className)} {...rest}>{children}</p>;
}

export function SettingsFieldGrid({ children, className, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("grid gap-4 md:grid-cols-2", className)} {...rest}>{children}</div>;
}

interface SettingsFieldProps {
  /** The id of the field the label names; the helper and the error are tied to it by aria-describedby on the field. */
  id: string;
  label: React.ReactNode;
  /** The one-line helper, always visible under the field. */
  helper?: React.ReactNode;
  /** The bubble beside the label: what it is, the unit, an example, who reads it, what happens when it is empty. */
  info?: React.ReactNode;
  /** A small action at the end of the label row (a "Reset to default" link), never the field itself. */
  action?: React.ReactNode;
  /** The inline error, shown instead of the helper and announced with the field. */
  error?: React.ReactNode;
  required?: boolean;
  /** Spans both columns of a SettingsFieldGrid. */
  wide?: boolean;
  className?: string;
  children: React.ReactNode;
}

/** The id of the helper or error line under a field, for its aria-describedby. */
export const settingsFieldHintId = (id: string) => `${id}-hint`;
/** The id of a field's label, for a control named by aria-labelledby (a Select trigger). */
export const settingsFieldLabelId = (id: string) => `${id}-label`;

/**
 * After a refused Save, moves focus to the first field of `ids` that is set
 * (pass `error && id` in the form's order), once the errors are drawn, and
 * scrolls it to the middle of its scroll container so the sticky
 * SettingsSaveBar never hides the field or its error line.
 */
export function focusFirstInvalidField(ids: readonly (string | false | null | undefined)[]) {
  const first = ids.find((id): id is string => Boolean(id));
  if (!first) return;
  window.setTimeout(() => {
    const field = document.getElementById(first);
    if (!field) return;
    field.focus({ preventScroll: true });
    field.scrollIntoView?.({ block: "center", inline: "nearest" });
  }, 0);
}

type ControlProps = { id?: string; "aria-invalid"?: unknown };

/** The label of a field with its required marker and its info bubble. */
function SettingsFieldLabel({
  id,
  label,
  info,
  required,
  disabled,
}: Pick<SettingsFieldProps, "id" | "label" | "info" | "required"> & { disabled?: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center gap-1.5">
      <Label
        id={settingsFieldLabelId(id)}
        htmlFor={id}
        data-disabled={disabled ? "" : undefined}
        className={cn("flex items-center gap-1", disabled ? "text-default-secondary" : "text-default-primary")}
      >
        {label}
        {required && (
          <span className="text-feedback-error-primary" aria-label={t_i18n("Required")}>
            *
          </span>
        )}
      </Label>
      {info && <InfoTip content={<div className="max-w-xs space-y-1 content-compact">{info}</div>} label={t_i18n("About this field")} />}
    </div>
  );
}

export function SettingsField({ id, label, helper, info, action, error, required, wide, className, children }: SettingsFieldProps) {
  const hint = error || helper;
  // The control named by the label carries the error state itself, so the
  // invalid border shows and a screen reader announces it with the field.
  const control =
    error && React.isValidElement<ControlProps>(children) && children.props.id === id && children.props["aria-invalid"] === undefined
      ? React.cloneElement(children, { "aria-invalid": true })
      : children;
  return (
    <div className={cn("min-w-0 space-y-1.5", wide && "md:col-span-2", className)} data-slot="settings-field">
      {action ? (
        <div className="flex items-center justify-between gap-2">
          <SettingsFieldLabel id={id} label={label} info={info} required={required} />
          <div className="flex shrink-0 items-center">{action}</div>
        </div>
      ) : (
        <SettingsFieldLabel id={id} label={label} info={info} required={required} />
      )}
      {control}
      {hint && (
        <p
          id={settingsFieldHintId(id)}
          role={error ? "alert" : undefined}
          className={cn("content-compact", error ? "text-feedback-error-primary" : "text-default-secondary")}
        >
          {hint}
        </p>
      )}
    </div>
  );
}

interface SettingsSwitchFieldProps {
  /** The id of the switch the label names. */
  id: string;
  label: React.ReactNode;
  /** The one-line helper under the label. */
  helper?: React.ReactNode;
  /** The bubble beside the label: what it does when on and when off, who reads it. */
  info?: React.ReactNode;
  checked: boolean;
  disabled?: boolean;
  onCheckedChange: (checked: boolean) => void;
  className?: string;
  /** The fields the switch governs, shown under it. */
  children?: React.ReactNode;
}

/** A setting that is a switch: label, info bubble and helper on the left, the switch at the end, its own fields under it. */
export function SettingsSwitchField({ id, label, helper, info, checked, disabled, onCheckedChange, className, children }: SettingsSwitchFieldProps) {
  return (
    <div className={cn("min-w-0 space-y-3", className)} data-slot="settings-field">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-1.5">
          <SettingsFieldLabel id={id} label={label} info={info} disabled={disabled} />
          {helper && (
            <p id={settingsFieldHintId(id)} className="content-compact text-default-secondary">
              {helper}
            </p>
          )}
        </div>
        <Switch
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={helper ? settingsFieldHintId(id) : undefined}
          checked={checked}
          disabled={disabled}
          onCheckedChange={onCheckedChange}
        />
      </div>
      {children}
    </div>
  );
}

interface SettingsSaveBarProps {
  dirty: boolean;
  saving?: boolean;
  /** Save is refused as it stands (nothing changed, a field too long): the button is disabled. */
  saveDisabled?: boolean;
  /** Called on Save; omitted when Save submits the enclosing form (`submit`). */
  onSave?: () => void;
  /** Save is the submit button of the enclosing `<form>`, so Enter and its onSubmit save. */
  submit?: boolean;
  onCancel: () => void;
}

/** Save and Cancel of an open form, pinned to the bottom of the scroll area; says when changes are unsaved. */
export function SettingsSaveBar({ dirty, saving, saveDisabled, onSave, submit, onCancel }: SettingsSaveBarProps) {
  const { t_i18n } = useFormatter();
  return (
    <div
      data-testid="settings.save-bar"
      className="sticky bottom-0 z-10 flex items-center justify-end gap-3 border-t border-elevation-subtle bg-elevation-default px-4 py-3"
    >
      {dirty && (
        <span className="mr-auto content-compact text-default-secondary" role="status">
          {t_i18n("Unsaved changes")}
        </span>
      )}
      <Button type="button" variant="secondary" onClick={onCancel} disabled={saving}>
        {t_i18n("Cancel")}
      </Button>
      <Button
        type={submit ? "submit" : "button"}
        className="gap-1.5"
        onClick={submit ? undefined : onSave}
        disabled={saving || saveDisabled}
        aria-busy={saving || undefined}
      >
        {saving && <Spinner size="sm" tone="inherit" />}
        {!saving && <Icon name="check" size={16} />}
        {t_i18n("Save")}
      </Button>
    </div>
  );
}
