// SPDX-License-Identifier: Apache-2.0

/**
 * The log-in-once hint of a stage (#2042): the member's newest stage run was
 * skipped before any model call because they have no account on these
 * modules yet. Signing in once creates it; the next generation then reads it.
 * One implementation for the command center stage card and the stage page.
 */
import { useFormatter } from "@/i18n";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { formatList } from "@/lib/format-list";
import { cn } from "@/lib/utils";

export function StageSignInHint({
  modules,
  className,
}: {
  modules: readonly string[];
  /** Type scale of the host: the card strip's caption by default. */
  className?: string;
}) {
  const { t_i18n, locale } = useFormatter();
  return (
    <p
      className={cn("text-default-secondary", className ?? "content-caption my-auto")}
      data-testid="ctem-stage-sign-in-hint"
    >
      {modules.length > 0
        ? t_i18n("Sign in to {modules} once, then regenerate: your assessment reads your own account there.", {
            modules: formatList(
              modules.map((m) => CTEM_PRODUCT_INFO[m]?.name ?? m),
              locale,
            ),
          })
        : t_i18n("Sign in to OpenCTI or OpenAEV once, then regenerate: your assessment reads your own account there.")}
    </p>
  );
}
