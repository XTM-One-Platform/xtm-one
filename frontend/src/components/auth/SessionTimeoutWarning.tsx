// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { signOut } from "@/hooks/useAuth";
import { useSessionTimeout } from "@/hooks/useSessionTimeout";
import { useFormatter } from "@/i18n";

/**
 * Follows the session's absolute lifetime and idle timeout on every
 * signed-in page, and warns shortly before an idle sign-out. Any input while
 * the warning shows keeps the session, closing the dialog included.
 */
export function SessionTimeoutWarning() {
  const { t_i18n } = useFormatter();
  const qc = useQueryClient();
  const { warningSecondsLeft, staySignedIn } = useSessionTimeout();
  const open = warningSecondsLeft !== null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) staySignedIn();
      }}
    >
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-alert-primary/10">
              <Icon name="clock" size={20} className="text-feedback-warning-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">{t_i18n("Are you still there?")}</DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {t_i18n(
                  "You will be signed out in {seconds, plural, one {# second} other {# seconds}} because of inactivity.",
                  { seconds: warningSecondsLeft ?? 0 },
                )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" size="sm" onClick={() => signOut(qc)}>
            {t_i18n("Sign out")}
          </Button>
          <Button size="sm" onClick={staySignedIn}>
            {t_i18n("Stay signed in")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
