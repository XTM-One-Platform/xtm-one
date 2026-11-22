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

  it("names every source a reader recognises", () => {
    expect(versionSourceName("intune")).toBe("Microsoft Intune");
    expect(versionSourceName("openaev")).toBe("OpenAEV");
    expect(versionSourceName("sbom")).toBe("SBOM");
    expect(versionSourceName("csv")).toBe("CSV import");
    expect(versionSourceName("agent:abc")).toBe("Agent");
    expect(versionSourceName("something-else")).toBe("something-else");
  });
});
