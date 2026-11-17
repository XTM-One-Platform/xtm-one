// SPDX-License-Identifier: Apache-2.0

/**
 * The one "Sample" marker of the UI: a greyed preview (a CTEM card, a dashboard
 * view, a briefing shown before the first real one) carries this badge and
 * nothing else to say it is not real data. The design-system `Chip`, neutral,
 * with the shipped "Sample" key; the reason a widget shows a sample is said by
 * its own caption or by `AiLockedFooter`, never by the badge.
 *
 * `sample-badge.test.tsx` fails on any other rendering of the "Sample" key.
 */
import { Chip } from "@filigran/design-system";

import { useFormatter } from "@/i18n";

export function SampleBadge({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  return <Chip label={t_i18n("Sample")} severity="neutral" className={className} />;
}
