// The "Affected assets" card of a case (#1176 slice 3b): one row per asset the
// visible issues name, with the version it runs from the asset inventory on an
// upgrade case, "Version source: none yet" otherwise, never a software inventory ask.

import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseAffectedAssets } from "@/hooks/api/useCtemCasesApi";
import { ASSET_NAME_CAP, CaseAffectedAssets, RUNNING_VERSION_CAP } from "./CaseAffectedAssets";

function draw(data: CtemCaseAffectedAssets | undefined, paired = false) {
  return renderWithProviders(
    <MemoryRouter>
      <CaseAffectedAssets data={data} paired={paired} />
    </MemoryRouter>,
  );
}

const FROM_INVENTORY: CtemCaseAffectedAssets = {
  version_source: "inventory",
  asset_count: 2,
  more_assets: 0,
  assets: [
    {
      name: "web-01",
      issue_count: 2,
      open_issue_count: 2,
      kev: true,
      max_cvss: 9.8,
      in_inventory: true,
      asset_id: "a1",
      versions: ["8.0.1", "unknown"],
      sources: ["intune", "sbom"],
      status: "below_fixed",
    },
    { name: "db-01", issue_count: 1, open_issue_count: 0, kev: false, max_cvss: null, in_inventory: false },
  ],
};

describe("CaseAffectedAssets (#1176 slice 3b)", () => {
  it("lists each asset with its running version, its status and its sources", () => {
    const { container } = draw(FROM_INVENTORY);
    const rows = screen.getAllByTestId("ctem-case-affected-asset-row");
    expect(rows).toHaveLength(2);
    const web = rows.find((r) => within(r).queryByText("web-01"))!;
    expect(within(web).getByText("8.0.1, Unknown")).toBeInTheDocument();
    expect(within(web).getByText("Below the fixed version")).toBeInTheDocument();
    // Sources as the shared marks of the Running versions card, names for screen readers (UX r2 B1 on #1324).
    const marks = within(web).getAllByTestId("ctem-case-version-source");
    expect(marks.map((m) => m.textContent)).toEqual(["Microsoft Intune", "SBOM"]);
    expect(within(web).queryByText("Microsoft Intune, SBOM")).toBeNull();
    const db = rows.find((r) => within(r).queryByText("db-01"))!;
    expect(within(db).getByText("Not in the inventory")).toBeInTheDocument();
    expect(screen.queryByTestId("ctem-case-affected-assets-no-version")).toBeNull();
    expect(container.textContent).not.toMatch(/tech stack/i);
  });

  it("opens the asset overview from an inventory asset, and nothing from an asset outside it (CTEM-20-UI)", () => {
    draw(FROM_INVENTORY);
    const rows = screen.getAllByTestId("ctem-case-affected-asset-row");
    const web = rows.find((r) => within(r).queryByText("web-01"))!;
    expect(within(web).getByRole("link").getAttribute("href")).toBe("/ctem/inventory/assets/a1");
    const db = rows.find((r) => within(r).queryByText("db-01"))!;
    expect(within(db).queryByRole("link")).toBeNull();
  });

  it("keeps the routed inventory row to one tab stop, the source marks included (UX r1 B1 on #1361)", () => {
    draw(FROM_INVENTORY);
    const rows = screen.getAllByTestId("ctem-case-affected-asset-row");
    const web = rows.find((r) => within(r).queryByText("web-01"))!;
    const focusable = Array.from(web.querySelectorAll<HTMLElement>("a[href], button, input, select, textarea, [tabindex]")).filter(
      (el) => el.tabIndex >= 0,
    );
    expect(focusable).toHaveLength(1);
    expect(focusable[0].tagName).toBe("A");
    for (const mark of within(web).getAllByTestId("ctem-case-version-source")) expect(mark.tabIndex).toBe(-1);
  });

  it("says the version source is none yet, with no version column, when the inventory gives none", () => {
    const { container } = draw({
      version_source: "none",
      asset_count: 1,
      more_assets: 0,
      assets: [{ name: "web-01", issue_count: 1, open_issue_count: 1, kev: false, max_cvss: 7.5 }],
    });
    expect(screen.getByTestId("ctem-case-affected-assets-no-version").textContent).toBe("Version source: none yet");
    expect(screen.queryByRole("columnheader", { name: /Running version/ })).toBeNull();
    expect(container.textContent).not.toMatch(/tech stack/i);
  });

  it("cuts a long asset name and version with a tooltip under a 4xl table, whole from it and when paired (UX r2 B4 on #2758)", () => {
    const longName = "lapt-fili-0001.corp.example-subsidiary.internal.net";
    const longData: CtemCaseAffectedAssets = {
      ...FROM_INVENTORY,
      assets: [{ ...FROM_INVENTORY.assets[0], name: longName, versions: ["18.20.4-lts.windows-x64-msi"], status: undefined }],
    };
    // Stacked (not pairable): capped below the 896 px container width, whole from it, so 1080 never scrolls sideways.
    const stacked = draw(longData);
    const name = within(stacked.container).getByText(longName);
    for (const cls of ASSET_NAME_CAP.split(" ")) expect(name.className).toContain(cls);
    expect(name.className).toContain("truncate");
    const version = within(stacked.container).getByText("18.20.4-lts.windows-x64-msi");
    for (const cls of RUNNING_VERSION_CAP.split(" ")) expect(version.className).toContain(cls);
    stacked.unmount();
    // Paired: the data already fits a half, drawn whole on one line, never cut.
    const paired = draw(longData, true);
    const pairedName = within(paired.container).getByText(longName);
    expect(pairedName.className).toContain("whitespace-nowrap");
    expect(pairedName.className).not.toContain("max-w-[14rem]");
  });

  it("draws nothing when no visible issue names an asset", () => {
    draw({ version_source: "none", asset_count: 0, more_assets: 0, assets: [] });
    expect(screen.queryByTestId("ctem-case-affected-assets")).toBeNull();
    draw(undefined);
    expect(screen.queryByTestId("ctem-case-affected-assets")).toBeNull();
  });
});
