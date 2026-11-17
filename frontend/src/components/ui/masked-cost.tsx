// SPDX-License-Identifier: Apache-2.0

import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

export const COST_DATA_UNAVAILABLE_I18N_KEY =
  "Cost data not available in current deployment mode";

const MASKED_COST_VALUE = "--$";

export function MaskedCost({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();

  return (
    <Tip content={t_i18n(COST_DATA_UNAVAILABLE_I18N_KEY)}>
      <span className={cn("cursor-help", className)}>{MASKED_COST_VALUE}</span>
    </Tip>
  );
}
