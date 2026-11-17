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

/**
 * Compare with default (#9836 R3b, design dev-docs/roi-outcomes.md 10.4): the
 * fields a customization changed, default on the left, current on the right,
 * at the top of the edit dialog that opened it. Display only.
 */
import { useFormatter } from "@/i18n";

interface RoiDiffField {
  /** English label, translated here. */
  label: string;
  default: string | number;
  current: string | number;
}

/** The fields that differ, in the order given. */
const changedFields = (fields: readonly RoiDiffField[]) => fields.filter((f) => String(f.default) !== String(f.current));

export function RoiDefaultDiff({ fields, line }: { fields: readonly RoiDiffField[]; line?: string | null }) {
  const { t_i18n } = useFormatter();
  const changed = changedFields(fields);
  return (
    <section aria-label={t_i18n("Changed from the default")} data-testid="roi-default-diff" className="space-y-2 rounded-sm border border-elevation-subtle-soft p-3">
      <p className="content-compact font-medium text-default-primary">{t_i18n("Changed from the default")}</p>
      {line && <p className="content-caption text-default-secondary">{line}</p>}
      {changed.length === 0 ? (
        <p className="content-compact text-default-secondary">{t_i18n("Nothing differs from the default.")}</p>
      ) : (
        <table className="w-full content-compact">
          <thead>
            <tr className="text-left text-default-secondary">
              <th scope="col" className="py-1 pr-3 font-normal">{t_i18n("Field")}</th>
              <th scope="col" className="py-1 pr-3 font-normal">{t_i18n("Default")}</th>
              <th scope="col" className="py-1 font-normal">{t_i18n("Current value")}</th>
            </tr>
          </thead>
          <tbody>
            {changed.map((f) => (
              <tr key={f.label}>
                <th scope="row" className="py-1 pr-3 text-left font-normal text-default-secondary">{t_i18n(f.label)}</th>
                <td className="py-1 pr-3 text-default-secondary">{f.default}</td>
                <td className="py-1 font-medium text-default-primary">{f.current}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
