// SPDX-License-Identifier: Apache-2.0

/**
 * Empty body of the command center "Recommended actions" card (#9413 UX B1).
 *
 * With AI locked the chat rows are dropped (D-2 of dev-docs/ctem-open-source.md)
 * and, on a posture where no deterministic rule fires, the list would be empty:
 * the card then explains itself and carries the one way forward of the lock
 * reason (`AiLockedFooter`), as the Digests card does (E1), never an empty body.
 */
import { Target } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { useFormatter } from "@/i18n";

export function RecommendedActionsEmpty({ lockReason }: { lockReason: AiLockReason | null }) {
  const { t_i18n } = useFormatter();
  // No `requiresAi`: EmptyState would hide the footer with AI off.
  return (
    <EmptyState
      variant="inset"
      icon={Target}
      title={t_i18n("No recommended action yet")}
      description={t_i18n("Actions drawn from your exposure posture show here when one applies.")}
    >
      <AiLockedFooter reason={lockReason} className="justify-center" />
    </EmptyState>
  );
}
