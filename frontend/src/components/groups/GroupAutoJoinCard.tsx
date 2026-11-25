// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useUpdateGroup } from "@/hooks/api/useGroupApi";
import { useFormatter } from "@/i18n";
import type { Group } from "@/types";

/**
 * Auto join on the group Overview (#1901): people who sign in through single
 * sign-on join the group at every sign-in (`services/group_auto_join.py`).
 * Styled like the AI access card beside it.
 */
export function GroupAutoJoinCard({ group, groupId }: { group: Group; groupId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const updateGroup = useUpdateGroup();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Icon name="user-plus" size={16} aria-hidden />
          {t_i18n("Auto join")}
        </CardTitle>
        <CardDescription>
          {t_i18n(
            "People who sign in through single sign-on join this group automatically, at every sign-in. Members added by hand, by SAML group mapping or by SCIM are kept as they are.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-center justify-between gap-4">
          <span className="content-compact text-default-primary">{t_i18n("Single sign-on users join this group")}</span>
          <Switch
            aria-label={t_i18n("Single sign-on users join this group")}
            checked={group.auto_join === true}
            disabled={updateGroup.isPending}
            onCheckedChange={(checked) =>
              updateGroup.mutate(
                { groupId, data: { auto_join: checked } },
                {
                  onSuccess: () => toast({ title: t_i18n("Auto join updated"), variant: "success" }),
                  onError: (err) =>
                    toast({ title: t_i18n("Auto join could not be updated"), description: err.message, variant: "error" }),
                },
              )
            }
          />
        </div>
      </CardContent>
    </Card>
  );
}
