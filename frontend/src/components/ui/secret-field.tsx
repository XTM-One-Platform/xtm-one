// SPDX-License-Identifier: Apache-2.0

/**
 * SecretField: the one input for a credential the platform stores.
 *
 * A value is typed in a masked input and never shown again once saved. Where
 * the field accepts a vault reference, "From a vault" opens the secret picker
 * and the field then holds `vault://<provider>/<path>#<key>`, shown as a chip;
 * a reference stored before (picked, or typed by hand) is shown the same way,
 * from the entity's `vault_secrets.locations`. Pasting or typing a reference
 * stays possible, for a vault that does not let the user list it: the input
 * turns into a chip once the user leaves it, never while they type.
 *
 * `value` is the draft: `""` keeps what is stored. A stored secret (a value or
 * a reference) can always be replaced by typing; with `onClear` it can also be
 * cleared, `cleared` then says it will be removed and `onUndoClear` keeps it.
 * `concealed={false}` is for a credential that is not secret by itself (a
 * sign-in username): shown as typed, no reveal toggle.
 */
import { useMemo, useState, type Ref } from "react";
import { Eye, EyeOff, KeyRound, Undo2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tip } from "@/components/ui/tooltip";
import { VaultReferenceChip, VaultSecretPicker } from "@/components/ui/vault-picker";
import { usePickerVaults } from "@/hooks/api/useSecretVaultApi";
import { useFormatter } from "@/i18n";
import { isVaultReference, parseVaultReference } from "@/lib/secret-vaults";
import { cn } from "@/lib/utils";
import type { VaultSecretLocation } from "@/types";

export interface SecretFieldProps {
  label?: string;
  /** The accessible name when the label is drawn elsewhere. */
  "aria-label"?: string;
  id?: string;
  value: string;
  onChange: (value: string) => void;
  /** Where the stored secret reads from, when it is a vault reference (`vault_secrets.locations[field][0]`). */
  stored?: VaultSecretLocation | null;
  /** A secret is stored for this field (a value or a reference). */
  hasStoredValue?: boolean;
  /** Clear the stored secret; without it, a stored secret can only be replaced. */
  onClear?: () => void;
  /** The stored secret will be removed on save (after `onClear`). */
  cleared?: boolean;
  /** Keep the stored secret after all. */
  onUndoClear?: () => void;
  /** The field takes a vault reference (default). A field that does not shows a masked input only. */
  allowVault?: boolean;
  /** Masked with a reveal toggle (default); `false` for a credential shown as typed (a username). */
  concealed?: boolean;
  placeholder?: string;
  helperText?: string;
  error?: string;
  disabled?: boolean;
  className?: string;
  autoComplete?: string;
  /** The masked input, for a form that focuses it. */
  inputRef?: Ref<HTMLInputElement>;
  "aria-describedby"?: string;
  /** Set by a SettingsField that shows an inline error for this field: the invalid border and the announcement. */
  "aria-invalid"?: boolean;
  "aria-required"?: boolean;
}

export function SecretField({
  label,
  "aria-label": ariaLabel,
  id,
  value,
  onChange,
  stored,
  hasStoredValue,
  onClear,
  cleared = false,
  onUndoClear,
  allowVault = true,
  concealed = true,
  placeholder,
  helperText,
  error,
  disabled,
  className,
  autoComplete = "new-password",
  inputRef,
  "aria-describedby": describedBy,
  "aria-invalid": ariaInvalid,
  "aria-required": ariaRequired,
}: SecretFieldProps) {
  const { t_i18n } = useFormatter();
  const [shown, setShown] = useState(false);
  const [picking, setPicking] = useState(false);
  /** The user is typing in the input: a reference they type is not turned into a chip under their cursor. */
  const [typing, setTyping] = useState(false);
  /** The user chose to change a stored reference: the input (typed value, or From a vault) replaces the chip. */
  const [replacingStored, setReplacingStored] = useState(false);
  const { data: vaults } = usePickerVaults(allowVault);
  const canPick = allowVault && (vaults?.length ?? 0) > 0;

  const draftReference = useMemo(() => (isVaultReference(value) ? parseVaultReference(value) : null), [value]);
  const draftProvider = draftReference ? vaults?.find((entry) => entry.name === draftReference.provider) : undefined;
  const showStored = !value && !cleared && !replacingStored && Boolean(stored);
  const name = ariaLabel ?? label ?? t_i18n("Secret");
  // Named after its field, so a form with two secrets has two distinct toggles.
  const toggleName = shown ? t_i18n("Hide {name}", { name }) : t_i18n("Show {name}", { name });
  const fieldStored = Boolean(hasStoredValue || stored) && !cleared;

  const pick = (reference: string) => {
    setTyping(false);
    setReplacingStored(false);
    onChange(reference);
  };

  const chip =
    draftReference && !typing ? (
      <VaultReferenceChip
        providerName={draftProvider?.display_name ?? draftReference.provider}
        type={draftProvider?.type}
        path={draftReference.path}
        keyName={draftReference.key}
        onChange={canPick ? () => setPicking(true) : undefined}
        onClear={() => onChange("")}
        disabled={disabled}
      />
    ) : showStored && stored ? (
      <VaultReferenceChip
        providerName={stored.provider_display_name || stored.provider}
        type={vaults?.find((entry) => entry.name === stored.provider)?.type}
        path={stored.path}
        keyName={stored.key}
        onChange={() => setReplacingStored(true)}
        onClear={onClear}
        disabled={disabled}
      />
    ) : null;

  const storedPlaceholder = cleared
    ? t_i18n("Will be removed")
    : fieldStored
      ? t_i18n("Stored: type to replace")
      : placeholder;

  return (
    <div className={cn("space-y-1", className)}>
      {label && (
        <Label htmlFor={id} className="block text-default-primary">
          {label}
        </Label>
      )}
      {chip ?? (
        <div className="flex items-start gap-1.5">
          <Input
            ref={inputRef}
            id={id}
            aria-label={name}
            aria-describedby={describedBy}
            aria-invalid={ariaInvalid}
            aria-required={ariaRequired}
            type={concealed && !shown ? "password" : "text"}
            value={value}
            onChange={(event) => {
              setTyping(true);
              onChange(event.target.value);
            }}
            onBlur={() => setTyping(false)}
            placeholder={storedPlaceholder}
            disabled={disabled || cleared}
            autoComplete={autoComplete}
            className="flex-1"
            error={error}
          />
          {concealed && (
            <Tip content={toggleName}>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={toggleName}
                className="h-9 w-9 shrink-0"
                onClick={() => setShown(!shown)}
                disabled={disabled || cleared}
              >
                {shown ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </Button>
            </Tip>
          )}
          {cleared && onUndoClear ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-9 shrink-0 gap-1"
              onClick={onUndoClear}
              disabled={disabled}
            >
              <Undo2 className="h-3.5 w-3.5" aria-hidden />
              {t_i18n("Keep")}
            </Button>
          ) : (
            fieldStored &&
            !value &&
            onClear && (
              <Tip content={t_i18n("Clear the stored value")}>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t_i18n("Clear the stored value")}
                  className="h-9 w-9 shrink-0"
                  onClick={() => {
                    setReplacingStored(false);
                    onClear();
                  }}
                  disabled={disabled}
                >
                  <X className="h-4 w-4" />
                </Button>
              </Tip>
            )
          )}
          {canPick && !cleared && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-9 shrink-0 gap-1"
              onClick={() => setPicking(true)}
              disabled={disabled}
              data-testid="secret-field-from-vault"
            >
              <KeyRound className="h-3.5 w-3.5" aria-hidden />
              {t_i18n("From a vault")}
            </Button>
          )}
        </div>
      )}
      {helperText && !error && <p className="content-compact text-default-secondary">{helperText}</p>}
      {canPick && (
        <VaultSecretPicker
          open={picking}
          onOpenChange={setPicking}
          onPick={pick}
          initial={
            draftReference ??
            (stored?.path ? { provider: stored.provider, path: stored.path, key: stored.key } : stored ? { provider: stored.provider, path: "", key: null } : null)
          }
        />
      )}
    </div>
  );
}
