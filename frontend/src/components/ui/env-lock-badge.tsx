// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

/**
 * "Env" badge for settings seeded from environment variables (e.g.
 * DEFAULT_MODEL, DEFAULT_REASONING_EFFORT, LANGFUSE_*, env-seeded LLM
 * providers). Env always wins on startup, so editing these would silently
 * revert on the next restart — by default the badge signals the control is
 * read-only.
 *
 * Pass `tooltip` when only PART of the entity is env-managed (an env-seeded
 * LLM provider under `LLM_PROVIDER_MODELS_EDITABLE=true`: the API key is
 * env-managed, the model catalog is admin-curated) so the badge does not
 * claim a control is read-only when it is not.
 */
export function EnvLockBadge({ tooltip }: { tooltip?: string } = {}) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={tooltip ?? t_i18n("Configured via environment variable. Read-only.")}>
      <Badge
        variant="outline"
        className="gap-1 border-feedback-alert-primary/40 bg-feedback-alert-primary/10 content-caption font-medium text-feedback-warning-tertiary light:text-feedback-warning-tertiary"
      >
        <Icon name="lock" size={16} />Env
      </Badge>
    </Tip>
  );
}
