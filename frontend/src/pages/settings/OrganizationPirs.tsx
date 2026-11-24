// SPDX-License-Identifier: Apache-2.0

/**
 * The PIRs of the organization profile in read mode (#9615): each PIR by its
 * OpenCTI name, linked to its OpenCTI page, never by its id. A PIR OpenCTI no
 * longer has reads "No longer in OpenCTI: <last known name>", the state first so
 * no truncation hides it, and offers its removal; an OpenCTI that could not be
 * read leaves the last known names with one line and Retry. Past
 * eight chips, "+N more" expands in place (the shared ExpandableChipList, the
 * same list as the regions of the identity header).
 *
 * The chips are the shared Badge (`secondary` for a PIR OpenCTI lists,
 * `outline` for one it no longer has, whose edge is the guarded 3:1 neutral
 * tone), at the 14 px of the Regions chips on the same card (UX round 1 B1).
 * Guard: tests_unit/organization-pirs-badge.test.ts.
 *
 * While OpenCTI's list is on its way, a PIR saved without a name draws a
 * skeleton chip, never placeholder text; two PIRs that share a name are told
 * apart by the start of their id (#11589).
 *
 * Each PIR OpenCTI lists carries its details action (#1974): "N of 4 set" names how much of
 * its question, owner, review cadence and primary assets served is filled, and opens the
 * details dialog, so a PIR with nothing said about it stands out where it is picked.
 */

import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ExpandableChipList } from "@/components/ui/expandable-chip-list";
import { Skeleton } from "@/components/ui/skeleton";
import type { OrganizationPirsResponse } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { pirLink } from "@/lib/organization-profile";
import { type PirDetail, PIR_DETAIL_FIELDS, pirDetailFilled } from "@/lib/organization-reference";

type PirState = "known" | "missing" | "unknown" | "loading";

export interface PirLabel {
  id: string;
  /** The name to show: OpenCTI's, else the last known one, else "" (the state then names it). */
  name: string;
  /**
   * known: OpenCTI lists it; missing: OpenCTI answered without it; unknown: OpenCTI could not be read;
   * loading: OpenCTI's list is on its way and no name was saved with the PIR.
   */
  state: PirState;
}

/** The characters of an id that tell two PIRs of the same name apart. */
const SHORT_ID = 8;

/**
 * Each saved PIR with the name to show and its state, from OpenCTI's list and the names saved with
 * the profile; each id once, and a name two PIRs share followed by the start of the id (#11589).
 */
export function pirLabels(
  ids: string[],
  names: Record<string, string>,
  list: OrganizationPirsResponse | undefined,
  loading = false,
): PirLabel[] {
  const live = new Map((list?.pirs ?? []).map((pir) => [pir.id, pir.name]));
  const answered = !!list && !list.unavailable;
  const labels: PirLabel[] = [...new Set(ids)].map((id) => {
    const liveName = live.get(id);
    if (liveName) return { id, name: liveName, state: "known" };
    const saved = names[id] ?? "";
    if (!saved && loading && !list) return { id, name: "", state: "loading" };
    return { id, name: saved, state: answered ? "missing" : "unknown" };
  });
  return labels.map((pir, _i, all) => ({ ...pir, name: distinctName(pir, all) }));
}

/** *pir*'s name, followed by the start of its id when another PIR of *all* has the same name. */
export function distinctName(pir: { id: string; name: string }, all: { id: string; name: string }[]): string {
  if (!pir.name) return pir.name;
  const shared = all.some((other) => other.id !== pir.id && other.name === pir.name);
  return shared ? `${pir.name} (${pir.id.slice(0, SHORT_ID)})` : pir.name;
}

/**
 * The words of a PIR label, never its id. A missing PIR leads with its state, so
 * a chip cut at its width still says it (UX round 1 B2).
 */
export function usePirText() {
  const { t_i18n } = useFormatter();
  return (pir: PirLabel): string => {
    if (pir.state === "missing") return pir.name ? t_i18n("No longer in OpenCTI: {name}", { name: pir.name }) : t_i18n("PIR no longer in OpenCTI");
    return pir.name || t_i18n("PIR name not available");
  };
}

interface OrganizationPirsViewProps {
  pirs: PirLabel[];
  list: OrganizationPirsResponse | undefined;
  /** OpenCTI was asked and did not answer: the names shown are the last known ones. */
  unreadable: boolean;
  retrying: boolean;
  onRetry: () => void;
  /** Opens the form with this PIR taken out (nothing is saved until Save). */
  onRemove: (id: string) => void;
  describedBy?: string;
  /** The stored details by PIR id (#1974); with onDetails, each PIR shows its completeness. */
  details?: Record<string, PirDetail>;
  /** Opens the details dialog of a PIR, with the name its chip shows. */
  onDetails?: (id: string, name: string) => void;
}

/** OpenCTI could not be read: one line with Retry, the same in read and edit mode (#11589). */
export function PirsUnreadable({ retrying, onRetry }: { retrying: boolean; onRetry: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <p className="flex items-center gap-2 content-compact text-default-secondary" role="status" data-testid="settings.organization.pirs-unreadable">
      {t_i18n("OpenCTI could not be read, names may be out of date")}
      <Button variant="ghost" size="sm" className="h-6 px-1.5 content-compact" onClick={onRetry} disabled={retrying} aria-busy={retrying}>
        {t_i18n("Retry")}
      </Button>
    </p>
  );
}

/**
 * A PIR whose name is on its way: a chip-sized skeleton, announced once by the list (#11589).
 * It sits on an elevation-hover tile, the colour of the default skeleton fill, so it is drawn
 * as an outlined chip (the border of the outline Badge) to stay visible on that tile.
 */
function PirChipSkeleton() {
  return (
    <span data-testid="settings.organization.pir-chip-skeleton" className="inline-flex">
      <Skeleton className="h-6 w-40 border border-feedback-neutral-primary bg-transparent" />
    </span>
  );
}

/** The details action of a PIR chip: its completeness, a full PIR marked, the dialog on click (#1974). */
function PirDetailsButton({ name, filled, onClick }: { name: string; filled: number; onClick: () => void }) {
  const { t_i18n } = useFormatter();
  const count = t_i18n("{set} of {total} set", { set: filled, total: PIR_DETAIL_FIELDS });
  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-6 shrink-0 gap-1 px-1.5 content-compact"
      aria-label={t_i18n("Details of {name}, {count}", { name, count })}
      onClick={onClick}
      data-testid="settings.organization.pir-details-open"
    >
      <Icon name={filled === PIR_DETAIL_FIELDS ? "circle-check" : "list-checks"} size={14} aria-hidden />
      {count}
    </Button>
  );
}

export function OrganizationPirsView({ pirs, list, unreadable, retrying, onRetry, onRemove, describedBy, details, onDetails }: OrganizationPirsViewProps) {
  const { t_i18n } = useFormatter();
  const text = usePirText();
  const loading = pirs.some((pir) => pir.state === "loading");
  if (pirs.length === 0) {
    return <p className="content-base text-default-secondary" aria-describedby={describedBy}>{t_i18n("Not set")}</p>;
  }
  return (
    <div
      className="flex flex-col gap-2"
      data-testid="settings.organization.pirs"
      aria-describedby={describedBy}
      aria-busy={loading || undefined}
    >
      {loading && <span className="sr-only" role="status">{t_i18n("Loading the PIR names")}</span>}
      <ExpandableChipList
        items={pirs}
        getKey={(pir) => pir.id}
        ariaLabel={t_i18n("Priority intelligence requirements (PIRs)")}
        renderChip={(pir) => {
          if (pir.state === "loading") return <PirChipSkeleton />;
          const href = pir.state === "missing" ? "" : pirLink(list?.opencti_url, pir.id);
          const label = text(pir);
          return (
              <Badge variant={pir.state === "missing" ? "outline" : "secondary"} className="max-w-full gap-1 font-normal content-base" title={label}>
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer" className="truncate hover:underline">
                    {label}
                  </a>
                ) : (
                  <span className="truncate">{label}</span>
                )}
                {pir.state === "missing" && (
                  <Button variant="ghost" size="sm" className="h-6 px-1.5 content-compact" onClick={() => onRemove(pir.id)}>
                    {t_i18n("Remove")}
                  </Button>
                )}
                {pir.state !== "missing" && onDetails && (
                  <PirDetailsButton name={label} filled={pirDetailFilled(details?.[pir.id])} onClick={() => onDetails(pir.id, label)} />
                )}
              </Badge>
          );
        }}
      />
      {unreadable && <PirsUnreadable retrying={retrying} onRetry={onRetry} />}
    </div>
  );
}
