// SPDX-License-Identifier: Apache-2.0

/**
 * The public link of a shared entity, in its Share dialog: on / off at once,
 * copy, replace, and one option of what its visitors are offered (for a
 * deck or a video). Managers only.
 *
 * The link is the one way into the entity that is not a grant, so it sits
 * apart from the grants, and its switch applies at once - it is not part of
 * the draft the dialog's Save button writes. Turning it off or replacing it
 * asks first, because both break a link people already hold; the option asks
 * nothing, since turning it off breaks no address. An administrator can turn
 * public links off for the platform: the card then says so instead of
 * offering a switch that would be refused.
 *
 * What differs between entities is said by the caller: the sentence under
 * the title, the warning before turning it off, the admin notice for a kept
 * link, and the option (a deck's PDF download, a video's download).
 */
import type { LucideIcon } from "lucide-react";
import { Globe, RefreshCw } from "lucide-react";
import { Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { absoluteUrl } from "@/lib/base-path";

/** What the card reads of a link: the deck's and the video's link answers share these fields. */
export interface PublicLinkState {
  available: boolean;
  enabled: boolean;
  path: string | null;
  created_at: string | null;
  created_by_name: string | null;
}

/** One thing the link offers besides viewing, switched on its own. */
export interface PublicLinkOption {
  label: string;
  description: string;
  icon: LucideIcon;
  checked: boolean;
  onChange: (value: boolean) => Promise<unknown>;
  /** The toasts once it is on / off. */
  onToast: string;
  offToast: string;
  testId: string;
}

export interface PublicLinkCardProps {
  state: PublicLinkState | undefined;
  isLoading: boolean;
  isError: boolean;
  /** A change is being written: every control waits. */
  busy: boolean;
  /** A replacement is being issued: its button spins. */
  replacing: boolean;
  enable: () => Promise<unknown>;
  disable: () => Promise<unknown>;
  regenerate: () => Promise<unknown>;
  /** Under the title: who may open it and what they see. */
  description: string;
  /** Before turning it off: what its holders are told. */
  offConfirm: string;
  /** While the admin switch is off and the entity keeps its link. */
  adminOffKept: string;
  option?: PublicLinkOption;
  /** `presentation.share.publicLink`, `video.share.publicLink`. */
  testIdPrefix: string;
}

export function PublicLinkCard({
  state,
  isLoading,
  isError,
  busy,
  replacing,
  enable,
  disable,
  regenerate,
  description,
  offConfirm,
  adminOffKept,
  option,
  testIdPrefix,
}: PublicLinkCardProps) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const enabled = !!state?.enabled;
  const available = state?.available ?? true;
  const url = state?.path ? absoluteUrl(state.path) : null;

  const failed = (err: unknown) =>
    toast({
      title: t_i18n("Could not update the public link"),
      description: err instanceof Error ? err.message : undefined,
      variant: "error",
    });

  const turn = async (on: boolean) => {
    try {
      if (on) {
        await enable();
        toast({ title: t_i18n("Public link turned on"), variant: "success" });
        return;
      }
      const ok = await confirm({
        title: t_i18n("Turn off the public link?"),
        description: offConfirm,
        confirmLabel: t_i18n("Turn off"),
        cancelLabel: t_i18n("Cancel"),
        variant: "warning",
      });
      if (!ok) return;
      await disable();
      toast({ title: t_i18n("Public link turned off"), variant: "success" });
    } catch (err) {
      failed(err);
    }
  };

  const replace = async () => {
    const ok = await confirm({
      title: t_i18n("Replace the public link?"),
      description: t_i18n("The current link stops working at once: whoever has it will need the new one."),
      confirmLabel: t_i18n("Replace link"),
      cancelLabel: t_i18n("Cancel"),
      variant: "warning",
    });
    if (!ok) return;
    try {
      await regenerate();
      toast({ title: t_i18n("Public link replaced"), variant: "success" });
    } catch (err) {
      failed(err);
    }
  };

  const setOption = async (value: boolean) => {
    if (!option) return;
    try {
      await option.onChange(value);
      toast({ title: value ? option.onToast : option.offToast, variant: "success" });
    } catch (err) {
      failed(err);
    }
  };

  const since = state?.created_at ? intl.formatDate(state.created_at, { dateStyle: "medium" }) : null;
  const OptionIcon = option?.icon;

  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-hover p-3" data-testid={testIdPrefix}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Globe className="h-4 w-4 shrink-0 text-accent" aria-hidden />
          <div className="min-w-0">
            <p className="content-base font-medium">{t_i18n("Public link")}</p>
            <p className="text-[11px] text-default-secondary">{description}</p>
          </div>
        </div>
        {isLoading ? (
          <Spinner size="sm" label={t_i18n("Loading...")} />
        ) : (
          <Switch
            checked={enabled && available}
            onCheckedChange={(on) => void turn(on)}
            disabled={busy || isError || (!available && !enabled)}
            aria-label={t_i18n("Public link")}
            data-testid={`${testIdPrefix}.switch`}
          />
        )}
      </div>

      {isError && <p className="content-caption text-feedback-warning-primary">{t_i18n("The public link could not be loaded.")}</p>}

      {!available && (
        <p className="content-caption text-feedback-warning-primary">
          {enabled ? adminOffKept : t_i18n("An administrator has turned public links off on this platform.")}
        </p>
      )}

      {enabled && available && (
        <>
          {url ? (
            <div className="flex items-start gap-2 rounded-sm border border-accent/30 bg-elevation-default px-3 py-2">
              <Globe className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
              <code
                className="min-w-0 flex-1 break-all font-mono-plex text-[11px] leading-snug text-default-primary"
                data-testid={`${testIdPrefix}.url`}
              >
                {url}
              </code>
              <CopyIconButton value={url} label={t_i18n("Copy public link")} className="-my-1 shrink-0" />
            </div>
          ) : (
            <p className="content-caption text-feedback-warning-primary">
              {t_i18n("This link can no longer be shown. Replace it to get a new one.")}
            </p>
          )}
          {option && OptionIcon && (
            <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-default px-3 py-2">
              <div className="flex min-w-0 items-center gap-2">
                <OptionIcon className="h-3.5 w-3.5 shrink-0 text-default-secondary" aria-hidden />
                <div className="min-w-0">
                  <p className="content-compact font-medium">{option.label}</p>
                  <p className="text-[11px] text-default-secondary">{option.description}</p>
                </div>
              </div>
              <Switch
                checked={option.checked}
                onCheckedChange={(value) => void setOption(value)}
                disabled={busy}
                aria-label={option.label}
                data-testid={option.testId}
              />
            </div>
          )}
          <div className="flex items-center justify-between gap-2">
            <p className="min-w-0 text-[11px] text-default-secondary">
              {state?.created_by_name && since
                ? t_i18n("Turned on by {name} on {date}.", { name: state.created_by_name, date: since })
                : since
                  ? t_i18n("Turned on on {date}.", { date: since })
                  : null}
            </p>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void replace()}
              disabled={busy}
              aria-busy={replacing || undefined}
              className="h-7 shrink-0 gap-1.5 px-2 content-compact"
            >
              {replacing && <Spinner size="sm" tone="inherit" />}
              {!replacing && <RefreshCw className="h-3 w-3" aria-hidden="true" />}
              {t_i18n("Replace link")}
            </Button>
          </div>
        </>
      )}
      {ConfirmEl}
    </div>
  );
}
