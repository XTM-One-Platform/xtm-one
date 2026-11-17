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
 * ConfidentialityGateDialog — the "break the glass" interstitial.
 *
 * Shown when an administrator opens the Conversation files panel on a
 * conversation they do not own. Admins legitimately need this for support and
 * debugging, but the files are another person's private working material, so the
 * access is made deliberate rather than incidental:
 *
 *   * It explains *what* is behind the gate and *whose* it is, before any
 *     filename is fetched — the listing request does not fire until the
 *     acknowledgement succeeds.
 *   * It states plainly that the access is recorded, naming the audit trail. A
 *     gate that only warns teaches nothing; a gate that says "this is logged
 *     against your account" changes the decision.
 *   * The confirm control requires an explicit checkbox first, so the
 *     acknowledgement cannot be collected by a reflexive click on a primary
 *     button. This is the whole point of the pattern — the friction IS the
 *     feature.
 *
 * The server-side counterpart (``POST .../files/access-ack``) is what actually
 * authorizes anything; this dialog is the educational half. Neither is
 * sufficient alone — a UI-only gate would be bypassable by calling the API
 * directly, which is why the grant is minted and audited server-side.
 */

import { useEffect, useState } from "react";
import { AlertTriangle, ScrollText, ShieldAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";

interface ConfidentialityGateDialogProps {
  open: boolean;
  /** Called when the user dismisses without acknowledging. */
  onCancel: () => void;
  /** Called once the user has explicitly acknowledged. */
  onAcknowledge: () => void;
  /** True while the acknowledgement request is in flight. */
  pending?: boolean;
  /** Message from a failed acknowledgement, rendered inline. */
  error?: string | null;
  /** Conversation title, for orientation ("whose files am I about to open?"). */
  conversationTitle?: string | null;
}

export function ConfidentialityGateDialog({
  open,
  onCancel,
  onAcknowledge,
  pending = false,
  error = null,
  conversationTitle,
}: ConfidentialityGateDialogProps) {
  const { t_i18n } = useFormatter();
  const [confirmed, setConfirmed] = useState(false);

  // Reset on every open. Without this, a previously-ticked box would still be
  // ticked the next time the gate is raised — turning a deliberate decision
  // into a single click, which defeats the gate.
  useEffect(() => {
    if (open) setConfirmed(false);
  }, [open]);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !pending) onCancel();
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-alert-primary/10">
              <ShieldAlert className="h-5 w-5 text-feedback-warning-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">
                {t_i18n("You are about to open someone else's files")}
              </DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {conversationTitle
                  ? t_i18n(
                      "The conversation \"{title}\" belongs to another user. Its files may contain confidential material they shared with the agent in private.",
                      { title: conversationTitle },
                    )
                  : t_i18n(
                      "This conversation belongs to another user. Its files may contain confidential material they shared with the agent in private.",
                    )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3">
          <div className="flex items-start gap-2.5">
            <ScrollText className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
            <p className="content-compact leading-relaxed text-default-secondary">
              {t_i18n(
                "Opening this panel is recorded in the audit trail against your account, together with the conversation and its owner. The record is permanent.",
              )}
            </p>
          </div>
          <div className="flex items-start gap-2.5">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" />
            <p className="content-compact leading-relaxed text-default-secondary">
              {t_i18n(
                "Only continue if you need this for a support or debugging task you can justify. Use what you find for that purpose alone, and do not copy or share it further.",
              )}
            </p>
          </div>
        </div>

        <label className="flex cursor-pointer items-start gap-2.5 content-base">
          <Switch checked={confirmed} onCheckedChange={setConfirmed} className="mt-0.5" />
          <span className="leading-relaxed text-default-secondary">
            {t_i18n(
              "I have a legitimate need to view these files and I understand this access is logged.",
            )}
          </span>
        </label>

        {error && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 p-2 content-base text-feedback-error-primary"
          >
            <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
            <span className="leading-relaxed">{error}</span>
          </div>
        )}

        <div className="flex gap-2 pt-1">
          <Button
            variant="outline"
            className="flex-1"
            disabled={pending}
            onClick={onCancel}
          >
            {t_i18n("Cancel")}
          </Button>
          <Button
            className="flex-1"
            disabled={!confirmed || pending}
            onClick={onAcknowledge}
          >
            {pending ? t_i18n("Recording…") : t_i18n("View files")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
