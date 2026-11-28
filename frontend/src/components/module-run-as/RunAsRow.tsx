// SPDX-License-Identifier: Apache-2.0

/**
 * The "Runs as" row of a key-value grid (#2650, #2739, #2740): who a source or a scheduled pass acts as in a module,
 * and the administrator's Change. Rendered as a `dt` / `dd` pair, so it sits on the label and value columns of the
 * `dl` it is placed in, beside Runs on, Schedule or Open in. The one place the run-as is rendered (UX round 4 on #2743,
 * B1), for the sources XTM One provisions and for the OpenCRQ evidence health pass alike: the name truncates, the
 * button keeps its width and names what it changes for assistive tech.
 */
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";

export function RunAsRow({
  name,
  changeLabel,
  onChange,
  testId,
}: {
  /** The user's name as the reader sees it (`useRunAsUserName`). */
  name: string;
  /** The accessible name of Change: what it changes, e.g. "Change the run-as user of OpenCTI". */
  changeLabel: string;
  /** Opens the change dialog; absent for a reader, who sees the row without Change. */
  onChange?: () => void;
  /** The test id prefix: the value is `testId`, the button `${testId}-change`. */
  testId: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <>
      <dt className="text-default-secondary">{t_i18n("Runs as")}</dt>
      <dd className="flex min-w-0 items-center gap-2">
        <span className="min-w-0 truncate" data-testid={testId}>
          {name}
        </span>
        {onChange && (
          <Button variant="ghost" size="sm" className="shrink-0" onClick={onChange} aria-label={changeLabel} data-testid={`${testId}-change`}>
            {t_i18n("Change")}
          </Button>
        )}
      </dd>
    </>
  );
}
