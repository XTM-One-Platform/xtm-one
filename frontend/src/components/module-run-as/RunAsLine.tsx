// SPDX-License-Identifier: Apache-2.0

/**
 * "Runs as: <name>  Change" (#2650, #2739): who a source or a scheduled pass acts as in a module, and the
 * administrator's Change. The one place this line is rendered (UX round 1 on #2742, B1), for the sources XTM One
 * provisions and for the OpenCRQ evidence health pass alike: the name truncates, the button keeps its width and
 * names what it changes for assistive tech.
 */
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";

export function RunAsLine({
  name,
  changeLabel,
  onChange,
  testId,
}: {
  /** The user's name as the reader sees it (`useRunAsUserName`). */
  name: string;
  /** The accessible name of Change: what it changes, e.g. "Change the run-as user of OpenCTI". */
  changeLabel: string;
  /** Opens the change dialog; absent for a reader, who sees the line without Change. */
  onChange?: () => void;
  /** The test id prefix: the line is `testId`, the button `${testId}-change`. */
  testId: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="min-w-0 truncate text-default-secondary" data-testid={testId}>
        {t_i18n("Runs as: {user}", { user: name })}
      </span>
      {onChange && (
        <Button variant="ghost" size="sm" className="shrink-0" onClick={onChange} aria-label={changeLabel} data-testid={`${testId}-change`}>
          {t_i18n("Change")}
        </Button>
      )}
    </span>
  );
}
