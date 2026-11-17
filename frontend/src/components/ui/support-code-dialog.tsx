// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from "react";
import { AlertTriangle, LifeBuoy } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useRedeemSupportCode } from "@/hooks/api/useSettingsApi";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { describePlatformQuotaReset } from "@/lib/platform-quota-reset";

interface SupportCodeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Redeem a Filigran support code: a recharge of every agent failure pool, or a
 * reset of the platform-wide usage counters - the code says which, and
 * the toast reports what was applied.
 *
 * Deliberately the same shape as ``LicenseActivationDialog``: a support code
 * is verified against the very same trust anchor as a license — offline,
 * against the public half embedded in the image — so an admin who has
 * activated a license already knows this interaction.
 *
 * A refused code is a correctable input error, not an incident: the backend's
 * 400 ``detail`` is written for the admin (malformed, not signed by Filigran,
 * expired on a date, issued for another platform, already used on a date) and
 * is rendered inline so the dialog stays open with the paste intact.
 */
export function SupportCodeDialog({ open, onOpenChange }: SupportCodeDialogProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const redeem = useRedeemSupportCode();

  const submit = () => {
    const value = textareaRef.current?.value || code;
    if (!value.trim()) {
      setError(t_i18n("Paste the support code Filigran sent you."));
      return;
    }
    setError(null);
    redeem.mutate(value, {
      onSuccess: (result) => {
        setCode("");
        if (textareaRef.current) textareaRef.current.value = "";
        onOpenChange(false);
        if (result.kind === "platform_quota_reset") {
          // A code can be narrowed to some counters: name exactly the ones
          // the server reset, never "everything starts again".
          toast({
            variant: "success",
            title: t_i18n("Usage counters reset"),
            description: describePlatformQuotaReset(result, t_i18n),
          });
          return;
        }
        // A truncated sweep means some pools were NOT recharged. Saying
        // "every agent" then would be a lie the admin only discovers by
        // reading the card.
        toast({
          variant: result.truncated ? "info" : "success",
          title: t_i18n("Failure pools recharged"),
          description: result.truncated
            ? t_i18n("Too many counters to clear in one pass: some agents still have a spent allowance. Redeem again or check the server logs.")
            : t_i18n("Every agent starts again with a full failure allowance."),
        });
      },
      onError: (err: Error) =>
        setError(err.message || t_i18n("This support code could not be redeemed.")),
    });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) setError(null);
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/10">
              <LifeBuoy className="h-5 w-5 text-accent" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">{t_i18n("Enter a support code")}</DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {t_i18n("Paste the support code Filigran issued you. Depending on what it grants, it gives every agent a full failure allowance again, or resets this platform's usage counters. A code can only be used once.")}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-3 pt-2">
          <textarea
            ref={textareaRef}
            defaultValue={code}
            onChange={(e) => {
              setCode(e.target.value);
              if (error) setError(null);
            }}
            placeholder="XTMP-..."
            spellCheck={false}
            autoComplete="off"
            className="w-full h-28 rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base font-mono-plex break-all placeholder:text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus resize-none"
          />
          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 p-2 content-base text-feedback-error-primary"
            >
              <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
              <span className="leading-relaxed">{error}</span>
            </div>
          )}
          <Button className="w-full" onClick={submit} disabled={redeem.isPending}>
            {redeem.isPending ? t_i18n("Verifying...") : t_i18n("Redeem code")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
