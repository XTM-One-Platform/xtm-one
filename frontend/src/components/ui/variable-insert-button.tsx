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
 * VariableInsertButton — inserts a {{var_id.UUID}} reference at the textarea cursor.
 *
 * Place next to any textarea label (same row as PromptGenerator) to let users
 * quickly inject a variable reference from a searchable popover. Secret
 * variables are badged. "Create a secret variable from a vault secret" picks
 * a secret from a vault the user may use, creates the secret variable holding
 * its reference, and inserts that variable: free text never holds a
 * vault reference, so the value reaches the model call only and what a run
 * stores, traces and logs holds the marker.
 *
 * Usage:
 *   const ref = useRef<HTMLTextAreaElement>(null);
 *   <VariableInsertButton textareaRef={ref} value={text} onChange={setText} />
 *   <textarea ref={ref} value={text} onChange={e => setText(e.target.value)} />
 */

import { useState, useRef, type RefObject } from "react";
import { Spinner } from "@filigran/design-system";
import { Braces, KeyRound, Lock, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { VaultReferenceChip, VaultSecretPicker } from "@/components/ui/vault-picker";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useCustomVariablesEnabled } from "@/hooks/usePlatformConfig";
import { usePickerVaults } from "@/hooks/api/useSecretVaultApi";
import { useCreateVariable, useVariables } from "@/hooks/api/useVariableApi";
import { useFormatter } from "@/i18n";
import { parseVaultReference } from "@/lib/secret-vaults";
import { cn } from "@/lib/utils";

interface VariableInsertButtonProps {
  textareaRef: RefObject<HTMLTextAreaElement | HTMLInputElement | null>;
  value: string;
  onChange: (value: string) => void;
}

/** A variable name a picked secret suggests: its key, else the last segment of its path. */
function suggestedName(reference: string): string {
  const parsed = parseVaultReference(reference);
  if (!parsed) return "";
  return parsed.key || parsed.path.split("/").pop() || "";
}

function VaultVariableDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (variableId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const create = useCreateVariable();
  const { data: vaults } = usePickerVaults(open);
  const [reference, setReference] = useState("");
  const [name, setName] = useState("");
  const [picking, setPicking] = useState(false);
  const parsed = parseVaultReference(reference);
  const vault = parsed ? vaults?.find((entry) => entry.name === parsed.provider) : undefined;

  const close = () => {
    onOpenChange(false);
    setReference("");
    setName("");
  };

  const submit = () => {
    if (!parsed || !name.trim()) return;
    create.mutate(
      { name: name.trim(), value: reference, is_secret: true },
      {
        onSuccess: (variable: { id: string }) => {
          onCreated(variable.id);
          toast({ variant: "success", title: t_i18n("Secret variable created") });
          close();
        },
        onError: (error: Error) =>
          toast({ variant: "error", title: t_i18n("Could not create the variable"), description: error.message }),
      },
    );
  };

  return (
    <>
      <Dialog open={open && !picking} onOpenChange={(next) => !next && close()}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t_i18n("Create a secret variable from a vault secret")}</DialogTitle>
            <DialogDescription>
              {t_i18n(
                "The variable holds the reference, never the value: the platform reads the secret when a run needs it, and what the run stores shows a marker instead. Only you see the variable until you share it.",
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            {parsed ? (
              <VaultReferenceChip
                providerName={vault?.display_name ?? parsed.provider}
                type={vault?.type}
                path={parsed.path}
                keyName={parsed.key}
                onChange={() => setPicking(true)}
                onClear={() => setReference("")}
              />
            ) : (
              <Button type="button" variant="outline" className="gap-1" onClick={() => setPicking(true)}>
                <KeyRound className="h-4 w-4" aria-hidden /> {t_i18n("Pick a secret")}
              </Button>
            )}
            <Input
              label={t_i18n("Variable name")}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t_i18n("My API token")}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={close}>
              {t_i18n("Cancel")}
            </Button>
            <Button type="button" onClick={submit} disabled={!parsed || !name.trim() || create.isPending} aria-busy={create.isPending || undefined}>
              {create.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Create and insert")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      <VaultSecretPicker
        open={open && picking}
        onOpenChange={setPicking}
        initial={parsed}
        onPick={(picked) => {
          setReference(picked);
          if (!name.trim()) setName(suggestedName(picked));
        }}
      />
    </>
  );
}

export function VariableInsertButton({ textareaRef, value, onChange }: VariableInsertButtonProps) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");
  const popoverRef = useRef<HTMLDivElement>(null);
  // Resolve the security flag BEFORE issuing the variables query so the
  // network request is suppressed entirely when the platform has
  // DISABLE_CUSTOM_VARIABLES set — without this gate, the previous
  // unconditional ``useVariables()`` was still fetching and caching
  // ``/variables`` even though the popover never rendered, leaking
  // values through DevTools / the React Query cache.
  const customVariablesEnabled = useCustomVariablesEnabled();
  const { data: variables = [] } = useVariables({ enabled: customVariablesEnabled });
  const { data: vaults } = usePickerVaults(customVariablesEnabled && open);

  useClickOutside(popoverRef, () => {
    if (open) setOpen(false);
  });
  if (!customVariablesEnabled) return null;

  const filtered = variables.filter((v) => {
    if (!filter.trim()) return true;
    const q = filter.toLowerCase();
    return (
      v.name.toLowerCase().includes(q) ||
      (v.description ?? "").toLowerCase().includes(q)
    );
  });

  const handleInsert = (variableId: string) => {
    const insertion = `{{var_id.${variableId}}}`;
    const textarea = textareaRef.current;

    if (textarea) {
      const start = textarea.selectionStart ?? value.length;
      const end = textarea.selectionEnd ?? value.length;
      const newValue = value.substring(0, start) + insertion + value.substring(end);
      onChange(newValue);
      requestAnimationFrame(() => {
        textarea.selectionStart = textarea.selectionEnd = start + insertion.length;
        textarea.focus();
      });
    } else {
      onChange(value + insertion);
    }

    setOpen(false);
    setFilter("");
  };

  return (
    <div className="relative" ref={popoverRef}>
      <Tip content={t_i18n("Insert variable")}>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 gap-1 px-1.5 content-caption text-default-secondary hover:text-accent"
          onClick={() => setOpen(!open)}
          data-testid="variable-insert-button"
        >
          <Braces className="h-3 w-3" />
          {t_i18n("Variable")}
        </Button>
      </Tip>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-72 rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay">
          <div className="flex items-center gap-2 border-b border-elevation-subtle px-3 py-2">
            <Search className="h-3.5 w-3.5 text-default-secondary shrink-0" />
            <input
              type="text"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={t_i18n("Search variables...")}
              aria-label={t_i18n("Search variables...")}
              className="flex-1 bg-transparent content-compact outline-none placeholder:text-default-secondary"
              autoFocus
            />
          </div>
          <div className="max-h-52 overflow-y-auto">
            {filtered.length === 0 ? (
              <p className="px-3 py-4 text-center content-compact text-default-secondary">
                {variables.length === 0 ? t_i18n("No variables created yet") : t_i18n("No matching variables")}
              </p>
            ) : (
              filtered.map((v) => (
                <button
                  key={v.id}
                  type="button"
                  className={cn(
                    "flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors",
                    "hover:bg-elevation-hover",
                  )}
                  onClick={() => handleInsert(v.id)}
                >
                  {v.is_secret ? (
                    <Lock className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
                  ) : (
                    <Braces className="h-3.5 w-3.5 text-accent shrink-0" />
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="content-compact font-medium truncate">{v.name}</p>
                    {v.description && (
                      <p className="content-caption text-default-secondary truncate">{v.description}</p>
                    )}
                  </div>
                  {v.is_secret && (
                    <Badge variant="warning" className="content-caption shrink-0">
                      {t_i18n("Secret")}
                    </Badge>
                  )}
                </button>
              ))
            )}
          </div>
          {(vaults?.length ?? 0) > 0 && (
            <button
              type="button"
              className="flex w-full items-center gap-2 border-t border-elevation-subtle px-3 py-2 text-left content-compact text-accent hover:bg-elevation-hover"
              onClick={() => {
                setOpen(false);
                setCreating(true);
              }}
              data-testid="variable-from-vault"
            >
              <KeyRound className="h-3.5 w-3.5 shrink-0" aria-hidden />
              {t_i18n("Create a secret variable from a vault secret")}
            </button>
          )}
        </div>
      )}
      <VaultVariableDialog open={creating} onOpenChange={setCreating} onCreated={handleInsert} />
    </div>
  );
}
