// SPDX-License-Identifier: Apache-2.0

/**
 * The row that holds "Running versions" and "Affected assets" on a case page (#2750):
 * side by side, 50/50 and the same height, once the page is wide enough for both
 * tables to keep every column; stacked full width below that, or when one of the two
 * cards stands alone.
 *
 * The layout is decided once per case, from its data, never from the page of a table
 * that is showing: a pager, sort, search or filter click never moves the cards (UX r1 B1
 * on #2758). A case with a known fixed version adds Fixed version and Status columns, and
 * a case whose longest asset name, version or platform would not fit at half width, keeps
 * the stacked layout; every other case pairs from the breakpoint.
 */
import type { ReactNode } from "react";
import type { CtemCaseDetail } from "@/hooks/api/useCtemCasesApi";
import { cn } from "@/lib/utils";

/**
 * Two equal tracks that never grow past half, from a 96rem (1536 px) content width: it leaves
 * each table about 708 px, the width every column of both tables needs with en, fr, es and ja
 * headers. The case page reaches it at 1920 with the navbar open and from 1632 with the rail.
 * Whole class names, so Tailwind sees them.
 */
const PAIRED_COLUMNS = "@min-[96rem]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]";

/**
 * The longest texts a half-width table draws whole on one line, in characters. At 708 px the
 * Affected assets table keeps about 300 px for Open issues, KEV, Max CVSS and Sources, which
 * leaves the Asset and Running version cells about 400 px together (13 px text, about 7 px a
 * character, with cell padding); Running versions keeps the same budget for Version and Platform.
 */
export const PAIRED_MAX_CHARS = { asset: 32, version: 24, platform: 20 } as const;

type PairableCase = Pick<CtemCaseDetail, "remediation_kind" | "fixed_version" | "running_versions" | "affected_assets">;

function longest(texts: Iterable<string | null | undefined>): number {
  let max = 0;
  for (const text of texts) max = Math.max(max, text?.length ?? 0);
  return max;
}

/** Whether at least one of the two cards renders: Running versions on an upgrade case, Affected assets when an asset is named. */
export function versionsOrAssetsRender(kase: Pick<CtemCaseDetail, "remediation_kind" | "affected_assets">): boolean {
  return kase.remediation_kind === "upgrade" || (kase.affected_assets?.asset_count ?? 0) > 0;
}

/** Whether both cards render and every column of both tables fits at half width, read from the case's whole data. */
export function pairVersionsAndAssets(kase: PairableCase): boolean {
  if (kase.remediation_kind !== "upgrade") return false;
  const assets = kase.affected_assets;
  if (!assets || assets.asset_count === 0) return false;
  const versions = kase.running_versions;
  const fixKnown = Boolean(versions?.fixed_version ?? kase.fixed_version) || Object.keys(versions?.fixed_versions ?? {}).length > 0;
  if (fixKnown) return false;
  if (assets.assets.some((asset) => asset.status)) return false;
  const rows = versions?.versions ?? [];
  if (longest(assets.assets.map((asset) => asset.name)) > PAIRED_MAX_CHARS.asset) return false;
  // An asset's cell joins its versions with ", ", so the cell is measured whole.
  const assetVersions = assets.assets.map((asset) => (asset.versions ?? []).join(", "));
  if (longest([...assetVersions, ...rows.map((row) => row.version)]) > PAIRED_MAX_CHARS.version) return false;
  return longest(rows.map((row) => row.platform)) <= PAIRED_MAX_CHARS.platform;
}

export function CaseVersionsAssetsRow({ pairable, children }: { pairable: boolean; children: ReactNode }) {
  return (
    <div className="@container" data-testid="ctem-case-versions-assets" data-paired={pairable ? "" : undefined}>
      <div className={cn("grid gap-6 *:min-w-0", pairable && PAIRED_COLUMNS)}>{children}</div>
    </div>
  );
}
