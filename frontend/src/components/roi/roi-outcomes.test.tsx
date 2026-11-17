// The "Outcomes" card rules (#9536, design dev-docs/roi-outcomes.md 6.1):
// the unit picker's group order in both modes, the count modes a text agent
// may use, the draft checks, and SegmentedFilter's disabled option.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { SearchableSelect } from "@/components/ui/searchable-select";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import type { RoiUnit } from "@/hooks/api/useRoiCatalogApi";
import { renderWithProviders } from "@/testing/providers";
import {
  COUNT_MODE_MEANINGS,
  countModeOptions,
  draftOf,
  hasStructuredOutput,
  inputOf,
  isDraftError,
  REFUSAL_MESSAGES,
  saveRefusalOf,
  unitOptions,
} from "./roi-outcomes";

afterEach(cleanup);

const minutes = { manual_min_low: 20, manual_min_likely: 30, manual_min_high: 45, review_min: 5 };
const UNITS: RoiUnit[] = [
  { key: "threat_report_triaged", name: "Threat report triaged", role: "CTI analyst", rate_role: "analyst", builtin: true, archived_at: null, overridden: false, ...minutes },
  { key: "cve_assessed", name: "CVE assessed", role: "Analyst", rate_role: "analyst", builtin: true, archived_at: null, overridden: false, ...minutes },
  { key: "contract_reviewed", name: "Contract reviewed", role: "Legal counsel", rate_role: "legal", builtin: false, archived_at: null, version: 1, source: "", ...minutes },
  { key: "brief_written", name: "brief written", role: "", rate_role: "analyst", builtin: false, archived_at: null, version: 1, source: "", ...minutes },
  { key: "old_unit", name: "Old unit", role: "", rate_role: "analyst", builtin: false, archived_at: "2026-11-01T10:00:00Z", version: 1, source: "", ...minutes },
];
const id = (key: string) => key;

describe("saveRefusalOf", () => {
  const refused = (detail: unknown, status = 422) => Object.assign(new Error(String(detail)), { status, detail });

  it("puts an unknown or archived unit on the unit field, never the backend text", () => {
    const refusal = saveRefusalOf(refused("unit 'lab_ux_vendor_review' is not a known unit"));
    expect(refusal).toEqual({ field: "unit", message: REFUSAL_MESSAGES.unit });
    expect(refusal.message).not.toContain("lab_ux_vendor_review");
  });

  it("maps the output path and units per run refusals to their fields", () => {
    expect(saveRefusalOf(refused("count_path is required when units are counted per output item")).field).toBe("count_path");
    expect(saveRefusalOf(refused("units_per_count must be at least 1")).field).toBe("units_per_count");
  });

  it("falls back to one sentence for anything else", () => {
    expect(saveRefusalOf(refused("subject_id must be a UUID"))).toEqual({ field: null, message: REFUSAL_MESSAGES.save });
    expect(saveRefusalOf(refused("You cannot manage this agent.", 403))).toEqual({ field: null, message: REFUSAL_MESSAGES.save });
    expect(saveRefusalOf(new Error("network"))).toEqual({ field: null, message: REFUSAL_MESSAGES.save });
  });
});

describe("unitOptions", () => {
  it("lists custom units first in Copilot mode, security units first in XTM One mode, archived left out", () => {
    const copilot = unitOptions(UNITS, { isXtmOne: false, translate: id, locale: "en" });
    expect(copilot.map((o) => o.value)).toEqual(["brief_written", "contract_reviewed", "cve_assessed", "threat_report_triaged"]);
    expect(copilot.map((o) => o.group)).toEqual(["Custom", "Custom", "Security units", "Security units"]);
    const xtmOne = unitOptions(UNITS, { isXtmOne: true, translate: id, locale: "en" });
    expect(xtmOne.map((o) => o.value)).toEqual(["cve_assessed", "threat_report_triaged", "brief_written", "contract_reviewed"]);
  });
});

describe("count modes and drafts", () => {
  it("disables per output item without a JSON output schema, with its hint", () => {
    const text = countModeOptions(false).find((o) => o.key === "per_output_item");
    expect(text).toMatchObject({ disabled: true, disabledHint: "Needs a JSON output schema." });
    expect(countModeOptions(true).find((o) => o.key === "per_output_item")?.disabled).toBe(false);
    expect(hasStructuredOutput({ output_format: "markdown", output_schema: null })).toBe(false);
    expect(hasStructuredOutput({ output_format: "json", output_schema: { type: "object" } })).toBe(true);
  });

  it("falls back to per run when an inherited per-output-item declaration lands on a text agent", () => {
    const draft = draftOf({ unit: "cve_assessed", count_mode: "per_output_item", count_path: "result.cves", units_per_count: 1 }, false);
    expect(draft.count_mode).toBe("per_run");
  });

  it("names the wrong field, else sends the body of the mode", () => {
    expect(inputOf({ unit: "", count_mode: "per_run", count_path: "", units_per_count: "1" })).toMatchObject({ field: "unit" });
    expect(inputOf({ unit: "x", count_mode: "per_output_item", count_path: " ", units_per_count: "1" })).toMatchObject({ field: "count_path" });
    expect(inputOf({ unit: "x", count_mode: "per_run", count_path: "", units_per_count: "0" })).toMatchObject({ field: "units_per_count" });
    const body = inputOf({ unit: "x", count_mode: "per_output_item", count_path: " result.items ", units_per_count: "7" });
    expect(isDraftError(body)).toBe(false);
    expect(body).toEqual({ unit: "x", count_mode: "per_output_item", count_path: "result.items", units_per_count: 1 });
  });
});

describe("count mode meanings (UX #9545 B3)", () => {
  it("gives every count mode one line that starts with its own label", () => {
    for (const option of countModeOptions(true)) {
      expect(COUNT_MODE_MEANINGS[option.key].startsWith(`${option.label}: `)).toBe(true);
    }
  });
});

describe("SearchableSelect invalid state (UX #9545 B4)", () => {
  it("announces aria-invalid on the field it renders", () => {
    renderWithProviders(
      <SearchableSelect aria-label="Unit of work" aria-invalid options={[{ value: "a", label: "A" }]} value={null} onValueChange={() => {}} />,
    );
    expect(screen.getByLabelText("Unit of work").getAttribute("aria-invalid")).toBe("true");
  });

  it("leaves aria-invalid off a valid field", () => {
    renderWithProviders(
      <SearchableSelect aria-label="Unit of work" options={[{ value: "a", label: "A" }]} value={null} onValueChange={() => {}} />,
    );
    expect(screen.getByLabelText("Unit of work").getAttribute("aria-invalid")).toBeNull();
  });
});

describe("SegmentedFilter disabled option", () => {
  it("skips a disabled option with the arrow keys and shows its hint as text", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <SegmentedFilter selection="radio" ariaLabel="Count" options={countModeOptions(false)} value="per_run" onChange={onChange} />,
    );
    const perRun = screen.getByRole("radio", { name: "Per run" });
    const perItem = screen.getByRole("radio", { name: "Per output item" }) as HTMLButtonElement;
    expect(perItem.disabled).toBe(true);
    expect(screen.getByText("Needs a JSON output schema.")).toBeTruthy();
    expect(screen.getByRole("radiogroup").getAttribute("aria-describedby")).toBeTruthy();
    fireEvent.keyDown(perRun, { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith("per_work_item");
    fireEvent.click(perItem);
    expect(onChange).not.toHaveBeenCalledWith("per_output_item");
  });
});
