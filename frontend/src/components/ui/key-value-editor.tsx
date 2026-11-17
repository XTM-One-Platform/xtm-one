// SPDX-License-Identifier: Apache-2.0

import { useId, useRef, useState } from "react";
import { Eye, EyeOff, KeyRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { VaultReferenceChip, VaultSecretPicker } from "@/components/ui/vault-picker";
import { usePickerVaults } from "@/hooks/api/useSecretVaultApi";
import { useFormatter } from "@/i18n";
import { parseVaultReference } from "@/lib/secret-vaults";
import { randomId } from "@/lib/random-id";

interface KeyValuePair {
  key: string;
  value: string;
}

interface KeyValueEditorProps {
  pairs: KeyValuePair[];
  onChange: (pairs: KeyValuePair[]) => void;
  showSecrets: boolean;
  onToggleSecrets: (show: boolean) => void;
  /** Label shown above the editor (e.g. "Environment Variables", "HTTP Headers") */
  label: string;
  /** Optional hint shown after the label */
  hint?: string;
  /** The one-line help under the editor's title, tied to every key and value input. */
  helper?: string;
  /** The bubble beside the title: what the entries are, an example, who reads them, what happens when empty. */
  info?: string;
  /** Placeholder for the key input (defaults to index-based) */
  keyPlaceholders?: string[];
  /** Placeholder for the value input (defaults to index-based) */
  valuePlaceholders?: string[];
  /** Label for the key column */
  keyLabel?: string;
  /** Label for the value column */
  valueLabel?: string;
  /** Label for the "add" button */
  addLabel?: string;
  /** Optional per-row descriptions (e.g. from presets) */
  descriptions?: (string | undefined)[];
  /** Use compact sizing (for edit forms) */
  compact?: boolean;
  /** Each value takes a vault reference, picked with the secret picker. */
  allowVault?: boolean;
}

export function KeyValueEditor({
  pairs,
  onChange,
  showSecrets,
  onToggleSecrets,
  label,
  hint,
  helper,
  info,
  keyPlaceholders,
  valuePlaceholders,
  keyLabel = "Name",
  valueLabel = "Value",
  addLabel = "+ Add another",
  descriptions,
  compact = false,
  allowVault = false,
}: KeyValueEditorProps) {
  // Static labels/hints are translated centrally; technical placeholders
  // (e.g. "ENV_VAR_NAME") pass through untranslated.
  const { t_i18n } = useFormatter();
  const baseId = useId();
  const { data: vaults } = usePickerVaults(allowVault);
  const canPick = allowVault && (vaults?.length ?? 0) > 0;
  const [pickingRow, setPickingRow] = useState<number | null>(null);
  /** The row whose value is being typed: a reference typed there becomes a chip once the user leaves it (as in SecretField). */
  const [typingRow, setTypingRow] = useState<number | null>(null);
  // Stable per-row React keys: pairs have no natural identity (both fields are
  // editable), so generate an id per row instead of keying by array index.
  const rowIdsRef = useRef<string[]>([]);
  if (rowIdsRef.current.length > pairs.length) {
    rowIdsRef.current = rowIdsRef.current.slice(0, pairs.length);
  }
  while (rowIdsRef.current.length < pairs.length) {
    rowIdsRef.current.push(randomId());
  }

  const updatePair = (index: number, field: "key" | "value", newValue: string) => {
    const next = [...pairs];
    next[index] = { ...next[index], [field]: newValue };
    onChange(next);
  };

  const addPair = () => onChange([...pairs, { key: "", value: "" }]);

  const inputSize = compact ? "h-8 content-base" : "";
  const groupLabelId = `${baseId}-label`;
  const helperId = helper ? `${baseId}-helper` : undefined;

  return (
    <div className="space-y-2" role="group" aria-labelledby={groupLabelId}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span id={groupLabelId} className="content-base font-medium text-default-primary">
            {t_i18n(label)}
            {hint && <span className="content-compact font-normal text-default-secondary"> {t_i18n(hint)}</span>}
          </span>
          {info && <InfoTip content={<div className="max-w-xs space-y-1 content-compact">{t_i18n(info)}</div>} label={t_i18n("About this field")} />}
        </div>
        <Button variant="ghost" size="sm" className="h-6 px-2 content-compact" onClick={() => onToggleSecrets(!showSecrets)}>
          {showSecrets ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}
          {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
        </Button>
      </div>
      {helper && <p id={helperId} className="-mt-1 content-compact text-default-secondary">{t_i18n(helper)}</p>}
      <div className="space-y-2">
        {pairs.map((pair, idx) => {
          const rowId = `${baseId}-${rowIdsRef.current[idx]}`;
          const keyId = `${rowId}-key`;
          const valueId = `${rowId}-value`;
          const descriptionId = descriptions?.[idx] ? `${rowId}-description` : undefined;
          // Each input is described by the editor's help line and its row's preset description.
          const describedBy = [helperId, descriptionId].filter(Boolean).join(" ") || undefined;
          return (
          <div key={rowIdsRef.current[idx]}>
            {descriptions?.[idx] && (
              <p id={descriptionId} className="content-compact text-default-secondary mb-1.5">{t_i18n(descriptions[idx])}</p>
            )}
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label id={`${keyId}-label`} htmlFor={keyId} className="block text-default-primary">{t_i18n(keyLabel)}</Label>
                <Input id={keyId}
                  aria-labelledby={`${keyId}-label`}
                  aria-describedby={describedBy}
                  type={showSecrets ? "text" : "password"}
                  value={pair.key}
                  onChange={(e) => updatePair(idx, "key", e.target.value)}
                  placeholder={keyPlaceholders?.[idx] ?? t_i18n("Name")}
                  className={`font-mono-plex ${inputSize}`}
                />
              </div>
              <div className="space-y-1">
                <Label id={`${valueId}-label`} htmlFor={valueId} className="block text-default-primary">{t_i18n(valueLabel)}</Label>
                {(() => {
                  const reference = allowVault && typingRow !== idx ? parseVaultReference(pair.value) : null;
                  if (reference) {
                    const vault = vaults?.find((entry) => entry.name === reference.provider);
                    return (
                      <VaultReferenceChip
                        providerName={vault?.display_name ?? reference.provider}
                        type={vault?.type}
                        path={reference.path}
                        keyName={reference.key}
                        onChange={canPick ? () => setPickingRow(idx) : undefined}
                        onClear={() => updatePair(idx, "value", "")}
                      />
                    );
                  }
                  return (
                    <div className="flex items-center gap-1.5">
                      <Input id={valueId}
                        aria-labelledby={`${valueId}-label`}
                        aria-describedby={describedBy}
                        type={showSecrets ? "text" : "password"}
                        value={pair.value}
                        onChange={(e) => {
                          setTypingRow(idx);
                          updatePair(idx, "value", e.target.value);
                        }}
                        onBlur={() => setTypingRow(null)}
                        placeholder={valuePlaceholders?.[idx] ? t_i18n(valuePlaceholders[idx]) : t_i18n("secret value")}
                        className={`flex-1 ${inputSize}`}
                      />
                      {canPick && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className={compact ? "h-8 shrink-0 gap-1" : "h-9 shrink-0 gap-1"}
                          onClick={() => setPickingRow(idx)}
                        >
                          <KeyRound className="h-3.5 w-3.5" aria-hidden />
                          {t_i18n("From a vault")}
                        </Button>
                      )}
                    </div>
                  );
                })()}
              </div>
            </div>
          </div>
          );
        })}
        <Button variant="ghost" size="sm" className="content-compact text-default-secondary" onClick={addPair}>
          {t_i18n(addLabel)}
        </Button>
      </div>
      {canPick && (
        <VaultSecretPicker
          open={pickingRow !== null}
          onOpenChange={(open) => !open && setPickingRow(null)}
          onPick={(reference) => {
            setTypingRow(null);
            if (pickingRow !== null) updatePair(pickingRow, "value", reference);
          }}
          initial={pickingRow !== null ? parseVaultReference(pairs[pickingRow]?.value) : null}
        />
      )}
    </div>
  );
}
