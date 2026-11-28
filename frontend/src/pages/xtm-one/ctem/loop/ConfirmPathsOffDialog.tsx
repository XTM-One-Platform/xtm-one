// SPDX-License-Identifier: Apache-2.0

/**
 * The one confirmation of an off-switch that stops several trigger paths at once (#2784): the Provisioned switch of
 * a source XTM One provisions, the scheduled pass switch of a source's opened row, and the outside lens and evidence
 * health passes saved off from Settings. It names every path the off stops; turning on, or an off that stops one path
 * alone, asks nothing ("Use on this path" stops one path and never asks).
 */
import { type ReactNode, useState } from "react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useFormatter } from "@/i18n";

function ConfirmPathsOffDialog({
  open,
  title,
  paths,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  /** Already translated: names the switch or the source turned off. */
  title: string;
  /** The translated labels of the paths the off stops, the current one included. */
  paths: readonly string[];
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={t_i18n("{count, plural, one {# path stops receiving its signals:} other {# paths stop receiving its signals:}}", { count: paths.length })}
      confirmLabel="Turn off"
      variant="warning"
      onConfirm={onConfirm}
    >
      <ul className="flex flex-col gap-1 pl-[3.25rem] content-compact" data-testid="ctem-confirm-paths-off-list">
        {paths.map((label) => (
          <li key={label} className="list-disc">
            {label}
          </li>
        ))}
      </ul>
    </ConfirmDialog>
  );
}

/**
 * Guard one off: `guard(paths, title, run)` runs `run` at once when it stops one path or none, else asks first with
 * the dialog `element` the caller renders.
 */
export function useConfirmPathsOff(): [ReactNode, (paths: readonly string[], title: string, run: () => void) => void] {
  const [ask, setAsk] = useState<{ title: string; paths: readonly string[]; run: () => void } | null>(null);
  const guard = (paths: readonly string[], title: string, run: () => void) => {
    if (paths.length > 1) setAsk({ title, paths, run });
    else run();
  };
  const element = (
    <ConfirmPathsOffDialog
      open={ask !== null}
      title={ask?.title ?? ""}
      paths={ask?.paths ?? []}
      onOpenChange={(open) => {
        if (!open) setAsk(null);
      }}
      onConfirm={() => ask?.run()}
    />
  );
  return [element, guard];
}
