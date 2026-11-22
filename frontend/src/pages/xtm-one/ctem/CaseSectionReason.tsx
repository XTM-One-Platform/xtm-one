// SPDX-License-Identifier: Apache-2.0

/**
 * The empty state of a case page section (#1206, slice D), worded from live
 * state (`case-loop-reason.ts`): OpenCRQ not connected, no automation works
 * the case, the fleet run working it now, or the section's own sentence when
 * nothing blocks it. The page provides the state once (`CaseLoopProvider`);
 * a section rendered without it keeps its own sentence.
 */
import { createContext, useContext, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useFormatter } from "@/i18n";
import { sectionReason, type CaseLoopWorker } from "./case-loop-reason";

interface CaseLoopState {
  worker: CaseLoopWorker;
  /** Whether OpenCRQ is registered; `null` while unknown. */
  opencrq: boolean | null;
}

const CaseLoopContext = createContext<CaseLoopState>({ worker: { kind: "unknown" }, opencrq: null });

export function CaseLoopProvider({ value, children }: { value: CaseLoopState; children: ReactNode }) {
  return <CaseLoopContext.Provider value={value}>{children}</CaseLoopContext.Provider>;
}

const LINK_CLASS =
  "whitespace-nowrap text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";

export function CaseSectionReason({
  own,
  needsOpencrq = false,
  testId,
}: {
  /** The section's own sentence, already translated: shown when nothing blocks the section. */
  own: string;
  /** The section is written from OpenCRQ (exploitation reading, treatment plan). */
  needsOpencrq?: boolean;
  testId: string;
}) {
  const { t_i18n, intl } = useFormatter();
  const { worker, opencrq } = useContext(CaseLoopContext);
  const reason = sectionReason(worker, needsOpencrq, opencrq);

  if (reason === "opencrq") {
    return (
      <p className="text-default-secondary" data-testid={testId} data-reason={reason}>
        {t_i18n("Empty: OpenCRQ is not connected.")}{" "}
        <Link to="/integrations" className={LINK_CLASS}>
          {t_i18n("Connect OpenCRQ")}
        </Link>
      </p>
    );
  }
  if (reason === "not_set_up" && worker.kind === "not_set_up") {
    return (
      <p className="text-default-secondary" data-testid={testId} data-reason={reason}>
        {t_i18n("No automation works this case: the CTEM loop fleet is not set up.")}{" "}
        <Link to={worker.fleetId ? `/fleets/${worker.fleetId}` : "/fleets"} className={LINK_CLASS}>
          {t_i18n("Open the fleet")}
        </Link>
      </p>
    );
  }
  if (reason === "running" && worker.kind === "running") {
    const since = intl.formatDate(worker.since, { dateStyle: "medium", timeStyle: "short" });
    return (
      <p className="text-default-secondary" data-testid={testId} data-reason={reason}>
        {worker.agent
          ? t_i18n("Waiting for {agent}, working this case since {since}.", { agent: worker.agent, since })
          : t_i18n("Waiting for a fleet step, working this case since {since}.", { since })}
      </p>
    );
  }
  return (
    <p className="text-default-secondary" data-testid={testId} data-reason="own">
      {own}
    </p>
  );
}
