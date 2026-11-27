// SPDX-License-Identifier: Apache-2.0

import { useState, useCallback, useEffect, useId, useRef } from "react";
import { AlertTriangle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useFormatter } from "@/i18n";
import { challengeMatches, generateChallengeCode } from "@/lib/challenge-code";

// ---------------------------------------------------------------------------
// ConfirmDialog — a beautiful modal replacement for window.confirm()
// ---------------------------------------------------------------------------

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "danger" | "warning" | "default";
  /** The confirm button enables only once a random code shown in the dialog is typed back (`lib/challenge-code.ts`). */
  challenge?: boolean;
  /** Keeps the confirm button off while the decision cannot be made yet (a preview loading, a refusal shown). */
  confirmDisabled?: boolean;
  /** Content drawn under the description (a short list the decision rests on); already translated by the caller. */
  children?: React.ReactNode;
  /** `wide` for a decision that carries a form (several fields side by side); `sm` by default. */
  size?: "sm" | "wide";
  onConfirm: () => void;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title = "Are you sure?",
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "danger",
  challenge = false,
  confirmDisabled = false,
  children,
  size = "sm",
  onConfirm,
}: ConfirmDialogProps) {
  // Every string prop (title, labels, description) is translated once:
  // a static string is a catalog key and translates; a string the caller
  // already translated or built (a count, an entity name baked into the
  // text) is not a catalog key and shows as written, never looked up or
  // parsed as an ICU message a second time.
  const { t_i18n, t_text } = useFormatter();
  const codeId = useId();
  const [code, setCode] = useState("");
  const [typed, setTyped] = useState("");
  useEffect(() => {
    if (open && challenge) {
      setCode(generateChallengeCode());
      setTyped("");
    }
  }, [open, challenge]);
  const blocked = confirmDisabled || (challenge && !challengeMatches(code, typed));
  const confirm = () => {
    if (blocked) return;
    onConfirm();
    onOpenChange(false);
  };
  const variantStyles = {
    danger: {
      icon: "text-feedback-error-primary",
      iconBg: "bg-feedback-error-primary/10",
      button: "bg-feedback-error-primary hover:bg-feedback-error-primary/90 text-negative-primary border-0",
    },
    warning: {
      icon: "text-feedback-warning-primary",
      iconBg: "bg-feedback-alert-primary/10",
      button: "bg-feedback-alert-primary hover:bg-feedback-alert-primary/90 text-on-feedback-fill border-0",
    },
    default: {
      icon: "text-accent",
      iconBg: "bg-accent/10",
      button: "bg-accent hover:bg-accent/90 text-negative-primary border-0",
    },
  }[variant];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={size === "wide" ? "max-w-2xl" : "max-w-sm"}>
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${variantStyles.iconBg}`}>
              <AlertTriangle className={`h-5 w-5 ${variantStyles.icon}`} />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">{t_text(title)}</DialogTitle>
              {description && (
                <DialogDescription className="content-base leading-relaxed">
                  {t_text(description)}
                </DialogDescription>
              )}
            </div>
          </div>
        </DialogHeader>
        {children}
        {challenge && (
          <div className="space-y-2">
            <label htmlFor={codeId} className="content-compact text-default-secondary">
              {t_i18n("To confirm, type the code below:")}
            </label>
            <div
              className="select-all rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/5 px-3 py-2 text-center font-mono-plex text-lg font-semibold tracking-[0.35em] text-feedback-error-primary"
              data-testid="confirm-challenge-code"
            >
              {code}
            </div>
            <Input
              id={codeId}
              aria-label={t_i18n("To confirm, type the code below:")}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              placeholder={t_i18n("Type the code to enable the button")}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") confirm();
              }}
              className="font-mono-plex uppercase tracking-widest"
            />
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <Button
            variant="secondary"
            onClick={() => onOpenChange(false)}
          >
            {t_text(cancelLabel)}
          </Button>
          <Button
            className={variantStyles.button}
            disabled={blocked}
            onClick={confirm}
          >
            {t_text(confirmLabel)}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// useConfirm() — imperative hook that returns a `confirm()` async function
// Usage:
//   const [ConfirmEl, confirm] = useConfirm();
//   // In handler: if (await confirm({ title: "Delete?", description: "..." })) { ... }
//   // In JSX:    {ConfirmEl}
// ---------------------------------------------------------------------------

interface ConfirmOptions {
  title?: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "danger" | "warning" | "default";
  challenge?: boolean;
}

export function useConfirm(): [React.ReactNode, (opts?: ConfirmOptions) => Promise<boolean>] {
  const [state, setState] = useState<{ open: boolean; opts: ConfirmOptions }>({
    open: false,
    opts: {},
  });
  const resolveRef = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback((opts: ConfirmOptions = {}) => {
    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve;
      setState({ open: true, opts });
    });
  }, []);

  const handleOpenChange = useCallback((open: boolean) => {
    if (!open) {
      resolveRef.current?.(false);
      resolveRef.current = null;
    }
    setState((s) => ({ ...s, open }));
  }, []);

  const handleConfirm = useCallback(() => {
    resolveRef.current?.(true);
    resolveRef.current = null;
  }, []);

  const element = (
    <ConfirmDialog
      open={state.open}
      onOpenChange={handleOpenChange}
      onConfirm={handleConfirm}
      {...state.opts}
    />
  );

  return [element, confirm];
}
