// SPDX-License-Identifier: Apache-2.0

import { ColorPicker } from "@filigran/design-system";
import { InfoTip } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";

const HEX_COLOR_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

/**
 * A colour setting: the design-system ColorPicker draws the label, the help
 * line and the error itself, so it wires the label to its own field,
 * aria-invalid and aria-describedby (DS RULE-04). A non-parsing draft is
 * emitted verbatim by the picker, so the hex check below still flags it.
 */
export function ColorField({
  label, help, info, value, defaultValue, onChange,
}: {
  label: string;
  help: string;
  info: string;
  value: string;
  defaultValue?: string;
  onChange: (v: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const invalid = value !== "" && !HEX_COLOR_RE.test(value);
  return (
    <div className="min-w-0" data-slot="settings-field">
      <ColorPicker
        label={label}
        infoTooltip={<InfoTip content={<div className="max-w-xs space-y-1 content-compact">{info}</div>} label={t_i18n("About this field")} />}
        helperText={help}
        error={invalid ? t_i18n("Must be a hex color (#rgb, #rrggbb, or #rrggbbaa).") : undefined}
        value={value}
        onValueChange={onChange}
        placeholder={(defaultValue || "#000000").toLowerCase()}
        formats={["hex"]}
      />
    </div>
  );
}
