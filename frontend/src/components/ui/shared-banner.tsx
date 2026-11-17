// SPDX-License-Identifier: Apache-2.0

/**
 * Violet info banner for entities shared with the user by another user.
 *
 * What a share allows depends on its tier: `config` / `config_with_chats`
 * grantees may edit, `read_only` grantees may only look. Pass `readOnly` when
 * the share does not let the viewer edit, so the banner says so instead of
 * leaving an absent Edit button unexplained.
 *
 * Usage:
 *   <SharedBanner entityLabel="agent" ownerName="Alice" />
 *   <SharedBanner entityLabel="integration" readOnly />
 */

import { Lock, Users } from "lucide-react";
import { useFormatter } from "@/i18n";

interface SharedBannerProps {
  entityLabel: string;
  ownerName?: string | null;
  /** The viewer's share does not let them edit the entity. */
  readOnly?: boolean;
}

export function SharedBanner({ entityLabel, ownerName, readOnly = false }: SharedBannerProps) {
  const { t_i18n } = useFormatter();
  const entity = t_i18n(entityLabel);
  const Icon = readOnly ? Lock : Users;
  let message: string;
  if (readOnly) {
    message = ownerName
      ? t_i18n("This {entity} is shared with you by {name} with read-only access. Ask them for configuration access if you need to change it.", { entity, name: ownerName })
      : t_i18n("This {entity} is shared with you by another user with read-only access. Ask its owner for configuration access if you need to change it.", { entity });
  } else {
    message = ownerName
      ? t_i18n("This {entity} is shared with you by {name}.", { entity, name: ownerName })
      : t_i18n("This {entity} is shared with you by another user.", { entity });
  }
  return (
    <div className="rounded-sm bg-filigran-ia-primary/5 p-3 flex items-start gap-2.5">
      <Icon className="h-3.5 w-3.5 text-filigran-ia-primary shrink-0 mt-0.5" />
      <p className="content-compact text-filigran-ia-primary/90 leading-relaxed">{message}</p>
    </div>
  );
}
