// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import { IconButton, Spinner } from "@filigran/design-system";
import { Shield, Trash2, Mail, MailCheck, AlertCircle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/components/ui/toast";
import {
  useInvitations,
  useCreateInvitation,
  useRevokeInvitation,
} from "@/hooks/api/useInvitationsApi";
import { formatDateTime } from "@/lib/utils";

interface InviteUserDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

export function InviteUserDialog({ open, onOpenChange }: InviteUserDialogProps) {
  const { toast } = useToast();
  const { data: invitations } = useInvitations(open);
  const createInvitation = useCreateInvitation();
  const revokeInvitation = useRevokeInvitation();

  const [email, setEmail] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  // Outcome of the last send: the invitee gets the link by email; the admin
  // never sees the token/link.
  const [result, setResult] = useState<{ email: string; emailSent: boolean } | null>(null);

  // Reset transient state each time the dialog opens. Using ``useEffect``
  // (rather than a state-update during render) avoids the StrictMode
  // double-invocation warning and the render-loop hazard of mutating state
  // inside the render path — the prior ``if (open !== prevOpen) { setPrevOpen(open); ... }``
  // shape worked but tripped React's "Cannot update during render" guard in
  // dev and made the reset semantics harder to reason about across
  // re-renders triggered for unrelated reasons (parent prop changes, query
  // refetches, etc.).
  useEffect(() => {
    if (open) {
      setEmail("");
      setIsAdmin(false);
      setResult(null);
    }
  }, [open]);

  const handleSend = () => {
    const addr = email.trim();
    createInvitation.mutate(
      { email: addr, role: isAdmin ? "admin" : "user" },
      {
        onSuccess: (res) => {
          setResult({ email: res.email, emailSent: res.email_sent });
          if (res.email_sent) toast({ title: `Invitation sent to ${res.email}`, variant: "success" });
        },
        onError: (err) => {
          toast({ title: "Failed to create invitation", description: err.message, variant: "error" });
        },
      },
    );
  };

  // A valid pending invite already covers this email — block resending it.
  const trimmedEmail = email.trim().toLowerCase();
  const alreadyPending = !!invitations?.some(
    (inv) => inv.email.trim().toLowerCase() === trimmedEmail,
  );
  const canSend =
    isValidEmail(email.trim()) && !alreadyPending && !createInvitation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Invite a user</DialogTitle>
          <DialogDescription>
            We email the invitee a single-use link to create their account with the
            email and role you set here. The link is sent directly to them.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          {result ? (
            <div className="space-y-4">
              {result.emailSent ? (
                <div className="flex items-start gap-2.5 rounded-sm bg-accent/5 px-4 py-3">
                  <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
                  <p className="content-base text-accent">
                    Invitation email sent to <span className="font-semibold">{result.email}</span>.
                  </p>
                </div>
              ) : (
                <div className="flex items-start gap-2.5 rounded-sm bg-feedback-alert-primary/10 px-4 py-3">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" />
                  <p className="content-base text-feedback-warning-primary">
                    Invitation created for <span className="font-semibold">{result.email}</span>, but
                    the SMTP server refused the message. Check the server logs for the SMTP error and retry — revoke the pending invitation below if you can't deliver it.
                  </p>
                </div>
              )}
              <Button variant="outline" className="w-full" onClick={() => { setResult(null); setEmail(""); setIsAdmin(false); }}>
                Invite another
              </Button>
            </div>
          ) : (
            <>
              {/* Email */}
              <div className="space-y-1.5">
                <Label htmlFor="invite-user-email">Email</Label>
                <Input id="invite-user-email" aria-label="Email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="person@company.com"
                />
                {alreadyPending && (
                  <p className="flex items-center gap-1.5 content-caption text-feedback-warning-primary">
                    <AlertCircle className="h-3 w-3 shrink-0" />
                    A pending invitation already exists for this email. Revoke it below to resend.
                  </p>
                )}
              </div>

              {/* Role */}
              <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
                <div className="flex items-center gap-2">
                  <Shield className="h-4 w-4 text-accent" />
                  <div>
                    <p className="content-base font-medium">Administrator</p>
                    <p className="text-[11px] text-default-secondary">Grant admin access to manage the platform</p>
                  </div>
                </div>
                <Switch aria-label={"Administrator"} checked={isAdmin} onCheckedChange={setIsAdmin} />
              </div>

              <Button onClick={handleSend} disabled={!canSend} className="w-full gap-1.5" aria-busy={createInvitation.isPending || undefined}>
                {createInvitation.isPending && <Spinner size="sm" tone="inherit" />}{!createInvitation.isPending && <Mail className="h-4 w-4" />}
                Send invitation
              </Button>
            </>
          )}

          {/* Pending invitations */}
          {invitations && invitations.length > 0 && (
            <div className="space-y-2 border-t border-elevation-subtle-soft pt-4">
              <p className="content-compact font-medium text-default-secondary">Pending invitations</p>
              <div className="max-h-56 space-y-1.5 overflow-y-auto">
                {invitations.map((inv) => (
                  <div key={inv.id} className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-2">
                    <Badge variant={inv.role === "admin" ? "cyan" : "outline"} className="content-caption">{inv.role}</Badge>
                    <span className="flex-1 truncate content-compact">{inv.email}</span>
                    {inv.expires_at && (
                      <span className="hidden shrink-0 text-[11px] text-default-secondary sm:inline">
                        expires {formatDateTime(inv.expires_at)}
                      </span>
                    )}
                    <IconButton variant="default" priority="tertiary" aria-label="Revoke invitation" icon={<Trash2 className="h-3.5 w-3.5" />} className="h-7 w-7 shrink-0 text-default-secondary hover:text-feedback-error-primary" disabled={revokeInvitation.isPending} onClick={() => revokeInvitation.mutate(inv.id)} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
