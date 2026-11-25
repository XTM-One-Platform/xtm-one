// SPDX-License-Identifier: Apache-2.0

/**
 * The words of a case history event by its kind, English, translated where
 * drawn: the History timeline of the case page and the "Next step" of a gate
 * decision record (#1518) read the same table, so one event reads the same
 * on both.
 */
export const CASE_EVENT_LABEL: Readonly<Record<string, string>> = {
  created: "Created the case",
  issue_added: "Added an issue",
  issue_closed: "Closed an issue",
  // The folded upgrade target moved (CTEM-11, #894; shown by #928).
  retargeted: "Changed the target version",
  state: "Changed the state to",
  owner: "Changed the owner",
  due: "Changed the due date",
  note: "Added a note",
  split: "Split an issue out",
  merged: "Merged cases",
  // The store writes `merge` (ctem_cases_store.merge_cases, S5c).
  merge: "Merged cases",
  // The loop (CTEM-03, #857; shown by CTEM-03-UI, #892).
  signal: "A signal joined the case",
  validation: "Recorded a validation",
  sla_breached: "Escalated: SLA breached",
  // The case sweep found the case waiting on nobody: no gate, no reminder, no activity (#1736).
  stalled: "Stalled: waiting on nobody",
  // How the CVE would be exploited, read from OpenCRQ by triage (CTEM-24-UI, #936).
  exploitation: "Recorded how it would be exploited",
  // The attack-path reach of the affected asset, read from OpenCRQ by triage (CTEM-23-UI, #1289).
  attack_path: "Recorded the attack path",
  // An OpenAEV result a case waits on arrived (CTEM-15, #918; shown by CTEM-15-UI, #956).
  source_event: "OpenAEV result received",
  // The case ticket (CTEM-19, #917; shown by CTEM-19-UI, #1032): created, or its status moved.
  ticket: "Ticket status",
  // The ticket reached done and the sync asked for the re-test (CTEM-19-UI, #1032).
  retest_requested: "Re-test started: the ticket is done",
  // A loop gate of the fleet's catalog (CTEM-34, #975; shown by CTEM-17-UI).
  gate: "Gate",
  // The OpenCRQ treatment plan of the remediation decision (CTEM-18-UI, #1068).
  treatment_plan: "Attached the treatment plan",
  // The OpenCTI Grouping and coverage of the review step (CTEM-16-UI, #1115).
  threat_package: "Drafted the threat package",
  // A fix applied through a bound integration under ctem.production_change (#2061).
  applied_change: "Applied a change through an integration",
};
