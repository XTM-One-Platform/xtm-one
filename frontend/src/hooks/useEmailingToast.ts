// SPDX-License-Identifier: Apache-2.0

import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { minutesLeft } from "@/lib/model-announcements";
import type { ModelAnnouncementStatus } from "@/types";

/** What the admin is told once a model announcement is queued for the users. */
export function useEmailingToast() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  return (status: Pick<ModelAnnouncementStatus, "recipient_count" | "emails_per_minute">) =>
    toast({
      title: t_i18n("Emailing {count, plural, one {# user} other {# users}}", { count: status.recipient_count }),
      description: t_i18n("{rate} emails a minute, about {count, plural, one {# minute} other {# minutes}} in all. Follow it under User announcements.", {
        rate: status.emails_per_minute,
        count: minutesLeft(status.recipient_count, status.emails_per_minute),
      }),
      variant: "success",
    });
}
