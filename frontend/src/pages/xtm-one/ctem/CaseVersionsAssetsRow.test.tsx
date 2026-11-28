// The row of "Running versions" and "Affected assets" on a case page (#2750): side by
// side once both cards render and every column fits at half width, stacked otherwise.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { CtemCaseAffectedAssets, CtemCaseDetail } from "@/hooks/api/useCtemCasesApi";
import { CaseVersionsAssetsRow, pairVersionsAndAssets } from "./CaseVersionsAssetsRow";

type Kase = Pick<CtemCaseDetail, "remediation_kind" | "fixed_version" | "running_versions" | "affected_assets">;

const ASSETS: CtemCaseAffectedAssets = {
  version_source: "inventory",
  asset_count: 1,
  more_assets: 0,
  assets: [{ name: "lapt-0001", issue_count: 21, open_issue_count: 21, kev: true, max_cvss: 9.8, in_inventory: true, asset_id: "a1", versions: ["26.12.1"], sources: ["intune"] }],
};

const UPGRADE: Kase = {
  remediation_kind: "upgrade",
  fixed_version: null,
  running_versions: { versions: [], asset_count: 1, version_count: 1, fixed_version: null } as unknown as Kase["running_versions"],
  affected_assets: ASSETS,
};

describe("pairVersionsAndAssets (#2750)", () => {
  it("pairs an upgrade case with assets and no known fix", () => {
    expect(pairVersionsAndAssets(UPGRADE)).toBe(true);
  });

  it("keeps one card full width when the other does not render", () => {
    expect(pairVersionsAndAssets({ ...UPGRADE, remediation_kind: "patch" })).toBe(false);
    expect(pairVersionsAndAssets({ ...UPGRADE, affected_assets: undefined })).toBe(false);
    expect(pairVersionsAndAssets({ ...UPGRADE, affected_assets: { ...ASSETS, asset_count: 0, assets: [] } })).toBe(false);
  });

  it("stacks a case with a known fix, whose extra columns would not fit at half width", () => {
    expect(pairVersionsAndAssets({ ...UPGRADE, fixed_version: "26.13.0" })).toBe(false);
    expect(
      pairVersionsAndAssets({
        ...UPGRADE,
        running_versions: { ...UPGRADE.running_versions, fixed_versions: { windows: "26.13.0" } } as Kase["running_versions"],
      }),
    ).toBe(false);
    expect(
      pairVersionsAndAssets({ ...UPGRADE, affected_assets: { ...ASSETS, assets: [{ ...ASSETS.assets[0], status: "below_fixed" }] } }),
    ).toBe(false);
  });
});

describe("CaseVersionsAssetsRow (#2750)", () => {
  it("is paired only when pairable", () => {
    const { rerender } = render(
      <CaseVersionsAssetsRow pairable>
        <div />
        <div />
      </CaseVersionsAssetsRow>,
    );
    const row = screen.getByTestId("ctem-case-versions-assets");
    expect(row).toHaveAttribute("data-paired");

    rerender(
      <CaseVersionsAssetsRow pairable={false}>
        <div />
      </CaseVersionsAssetsRow>,
    );
    expect(screen.getByTestId("ctem-case-versions-assets")).not.toHaveAttribute("data-paired");
  });
});
