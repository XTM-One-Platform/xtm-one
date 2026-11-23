// The "Running versions" card of an upgrade case (#1252): the versions of the
// case's product the assets run, read from the asset inventory, each with its
// asset count, its status against the fixed version and its sources as logos
// (an SBOM gets a glyph and its name); no fixed version says so; no asset
// running the product says so; never a "type the version in the tech stack".

import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseRunningVersions } from "@/hooks/api/useCtemCasesApi";
import { CaseRunningVersions, versionSourceName } from "./CaseRunningVersions";

const WITH_FIX: CtemCaseRunningVersions = {
  product_label: "Microsoft Windows",
  fixed_version: "10.0.22631.4317",
  versions: [
    { version: "10.0.22621.2861", asset_count: 274, sources: ["intune", "defender"], status: "below_fixed" },
    { version: "10.0.22631.4317", asset_count: 640, sources: ["intune", "sbom"], status: "fixed" },
    { version: "unknown", asset_count: 1, sources: ["openaev"], status: "not_comparable" },
  ],
  more_versions: 0,
  version_count: 3,
  asset_count: 915,
  sources: ["defender", "intune", "openaev", "sbom"],
  assets_below_fixed: 274,
  assets_on_fixed: 640,
  assets_not_comparable: 1,
};

function draw(props: Parameters<typeof CaseRunningVersions>[0]) {
  return renderWithProviders(
    <MemoryRouter>
      <CaseRunningVersions {...props} />
    </MemoryRouter>,
  );
}

describe("CaseRunningVersions (#1252)", () => {
  it("lists every version with its assets, its status and its sources", () => {
    draw({ productLabel: "Microsoft Windows", fixedVersion: "10.0.22631.4317", versions: WITH_FIX });
    expect(screen.getByTestId("ctem-case-running-versions-summary").textContent).toBe(
      "The asset inventory reports Microsoft Windows on 915 assets in 3 versions.",
    );
    const against = screen.getByTestId("ctem-case-running-versions-against-fixed");
    expect(within(against).getByText("274 assets still below 10.0.22631.4317")).toBeInTheDocument();
    expect(within(against).getByText("640 assets run 10.0.22631.4317 or later")).toBeInTheDocument();
    expect(within(against).getByText("1 asset runs a version that cannot be compared")).toBeInTheDocument();

    const rows = screen.getAllByTestId("ctem-case-running-version-row");
    expect(rows).toHaveLength(3);
    const below = rows.find((r) => within(r).queryByText("10.0.22621.2861"))!;
    expect(within(below).getByText("Below the fixed version")).toBeInTheDocument();
    expect(within(below).getByText("Microsoft Intune")).toBeInTheDocument();
    expect(within(below).getByText("Microsoft Defender XDR")).toBeInTheDocument();
    const fixed = rows.find((r) => within(r).queryByText("10.0.22631.4317"))!;
    expect(within(fixed).getByText("SBOM")).toBeInTheDocument();
    const unknown = rows.find((r) => within(r).queryByText("Unknown"))!;
    expect(within(unknown).getByText("Cannot be compared")).toBeInTheDocument();
    expect(screen.queryByTestId("ctem-case-fixed-version-unknown")).toBeNull();
  });

  it("says no asset is below the fix at zero, never '0 assets'", () => {
    draw({
      productLabel: "Microsoft Windows",
      fixedVersion: "10.0.22631.4317",
      versions: { ...WITH_FIX, versions: [WITH_FIX.versions[1]], version_count: 1, asset_count: 640, assets_below_fixed: 0, assets_not_comparable: 0 },
    });
    expect(screen.getByText("No asset is below 10.0.22631.4317")).toBeInTheDocument();
    expect(screen.getByText("640 assets run 10.0.22631.4317 or later")).toBeInTheDocument();
    expect(screen.queryByText(/cannot be compared/)).toBeNull();
  });

  it("says the fixed version is unknown, with no status column, and never sends to the tech stack", () => {
    const noFix: CtemCaseRunningVersions = {
      ...WITH_FIX,
      fixed_version: null,
      versions: WITH_FIX.versions.map(({ status: _status, ...v }) => v),
      assets_below_fixed: undefined,
      assets_on_fixed: undefined,
      assets_not_comparable: undefined,
      fixed_version_checked: ["OpenCTI Software that remediates the vulnerabilities", "course of action naming a version"],
    };
    const { container } = draw({ productLabel: "Microsoft Windows", fixedVersion: null, versions: noFix });
    expect(screen.getByTestId("ctem-case-fixed-version-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("ctem-case-running-versions-against-fixed")).toBeNull();
    expect(screen.queryByText("Below the fixed version")).toBeNull();
    expect(container.textContent).not.toMatch(/tech stack/i);
  });

  it("says no asset reports the product when the inventory holds none", () => {
    draw({ productLabel: "F5 nginx", fixedVersion: "1.25.5", versions: null });
    expect(screen.getByTestId("ctem-case-running-versions-none").textContent).toBe(
      "No asset in the inventory reports F5 nginx: the running versions come from the assets (Intune, Defender, an SBOM, OpenAEV).",
    );
    expect(screen.queryAllByTestId("ctem-case-running-version-row")).toHaveLength(0);
  });

  it("reads the real total in the toolbar when versions are not listed, and draws nothing under the table (UX r1 B1 on #1277)", () => {
    const { container } = draw({
      productLabel: "Microsoft Windows",
      fixedVersion: "10.0.22631.4317",
      versions: { ...WITH_FIX, more_versions: 2, version_count: 5 },
    });
    expect(screen.getByTestId("data-table-total").textContent).toContain("3 of 5");
    expect(screen.getByRole("img", { name: "Versions not listed" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/more versions not listed/);
  });

  it("names each source in the product tooltip, never a native title (UX r1 B3 on #1277)", () => {
    draw({ productLabel: "Microsoft Windows", fixedVersion: "10.0.22631.4317", versions: WITH_FIX });
    for (const mark of screen.getAllByTestId("ctem-case-version-source")) {
      expect(mark.getAttribute("title")).toBeNull();
      expect(mark.getAttribute("tabindex")).toBe("0");
    }
  });

  it("names the asset column with its own key", () => {
    draw({ productLabel: "Microsoft Windows", fixedVersion: "10.0.22631.4317", versions: WITH_FIX });
    expect(screen.getByRole("columnheader", { name: /Asset count/ })).toBeInTheDocument();
  });

  // #1572 acceptance case: Windows fixed in 36.0, macOS in 37.0; one Windows asset runs 35.0 and 36.0.
  const PER_PLATFORM: CtemCaseRunningVersions = {
    product_label: "Google Chrome",
    fixed_version: "36.0",
    fixed_versions: { Windows: "36.0", macOS: "37.0" },
    versions: [
      { version: "36.0", platform: "Windows", asset_count: 4, asset_ids: ["w1", "w2", "w3", "w4"], more_asset_ids: 0, sources: ["intune"], status: "fixed", fixed_version: "36.0" },
      { version: "36.0", platform: "macOS", asset_count: 2, asset_ids: ["m1", "m2"], more_asset_ids: 0, sources: ["intune"], status: "below_fixed", fixed_version: "37.0" },
      { version: "35.0", platform: "Windows", asset_count: 1, asset_ids: ["w4"], more_asset_ids: 0, sources: ["defender"], status: "below_fixed", fixed_version: "36.0" },
      { version: "37.0", platform: "macOS", asset_count: 1, asset_ids: ["m3"], more_asset_ids: 0, sources: ["intune"], status: "fixed", fixed_version: "37.0" },
    ],
    more_versions: 0,
    version_count: 3,
    asset_count: 7,
    sources: ["defender", "intune"],
    platforms: ["Windows", "macOS"],
    assets_multi_version: 1,
    platform_unverified: false,
    assets_below_fixed: 3,
    assets_on_fixed: 4,
    assets_not_comparable: 0,
    assets_fix_not_known_for_platform: 0,
  };

  it("shows the platform and its fixed version per row, and the fix per platform (#1572)", () => {
    draw({ productLabel: "Google Chrome", fixedVersion: "36.0", versions: PER_PLATFORM });
    expect(screen.getByRole("columnheader", { name: /Platform/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Fixed version/ })).toBeInTheDocument();
    const rows = screen.getAllByTestId("ctem-case-running-version-row");
    expect(rows).toHaveLength(4);
    const macBelow = rows.find((r) => within(r).queryByText("macOS") && within(r).queryByText("36.0"))!;
    expect(within(macBelow).getByText("37.0")).toBeInTheDocument();
    expect(within(macBelow).getByText("Below the fixed version")).toBeInTheDocument();
    const against = screen.getByTestId("ctem-case-running-versions-against-fixed");
    expect(within(against).getByText("3 assets still below the fixed version of their platform")).toBeInTheDocument();
    expect(within(against).getByText("4 assets run the fixed version of their platform or later")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-fixed-versions-by-platform").textContent).toBe("Fixed version per platform: Windows: 36.0 and macOS: 37.0");
    expect(screen.getByTestId("ctem-case-running-versions-multi").textContent).toBe("1 asset runs several versions and is counted in each of its rows");
  });

  it("never reads a generic fix over several platforms as fixed (#1572)", () => {
    draw({
      productLabel: "Google Chrome",
      fixedVersion: "36.0",
      versions: {
        ...PER_PLATFORM,
        fixed_versions: undefined,
        platform_unverified: true,
        versions: [
          { version: "36.0", platform: "macOS", asset_count: 2, sources: ["intune"], status: "not_comparable", status_reason: "fix not known for this platform" },
          { version: "35.0", platform: "Windows", asset_count: 1, sources: ["intune"], status: "below_fixed" },
        ],
        assets_below_fixed: 1,
        assets_on_fixed: 0,
        assets_not_comparable: 2,
        assets_fix_not_known_for_platform: 2,
        assets_multi_version: 0,
      },
    });
    expect(screen.queryByRole("columnheader", { name: /Fixed version/ })).toBeNull();
    const mac = screen.getAllByTestId("ctem-case-running-version-row").find((r) => within(r).queryByText("macOS"))!;
    expect(within(mac).getByText("Fix not known for this platform")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-fix-platform-unverified").textContent).toBe(
      "36.0 names no platform while the assets run several: an asset not below it is not read as fixed.",
    );
    expect(screen.getByTestId("ctem-case-fix-not-known-for-platform").textContent).toBe("2 assets are on a platform with no known fix");
    expect(screen.queryByTestId("ctem-case-running-versions-multi")).toBeNull();
  });

  it("draws no platform column when the inventory names no platform", () => {
    draw({ productLabel: "Microsoft Windows", fixedVersion: "10.0.22631.4317", versions: WITH_FIX });
    expect(screen.queryByRole("columnheader", { name: /Platform/ })).toBeNull();
    expect(screen.queryByTestId("ctem-case-fixed-versions-by-platform")).toBeNull();
  });

  it("says one asset on several versions at a count of 1 (#1572)", () => {
    draw({ productLabel: "Google Chrome", fixedVersion: "36.0", versions: { ...PER_PLATFORM, assets_multi_version: 1 } });
    expect(screen.getByText("1 asset runs several versions and is counted in each of its rows")).toBeInTheDocument();
  });

  it("names every source a reader recognises", () => {
    expect(versionSourceName("intune")).toBe("Microsoft Intune");
    expect(versionSourceName("openaev")).toBe("OpenAEV");
    expect(versionSourceName("sbom")).toBe("SBOM");
    expect(versionSourceName("csv")).toBe("CSV import");
    expect(versionSourceName("agent:abc")).toBe("Agent");
    expect(versionSourceName("something-else")).toBe("something-else");
    // #1357: a module under its pre-rename key reads under its current name.
    expect(versionSourceName("openbas")).toBe("OpenAEV");
  });
});
