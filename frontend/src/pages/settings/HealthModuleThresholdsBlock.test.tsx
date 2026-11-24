// Settings > Platform Health, module thresholds (#101 S10c): the OpenCTI and
// OpenAEV (E) thresholds read at rest with their units, edited in the same
// draft, a value outside the server's bounds keeps Save off, and the block is
// absent in Copilot mode (no module there).
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { ModuleThresholdsBlock, moduleThresholdsValid } from "@/pages/settings/HealthModuleThresholdsBlock";
import { renderWithProviders } from "@/testing/providers";
import type { HealthSettings } from "@/types";

const mode = vi.hoisted(() => ({ xtmOne: true }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useIsXtmOne: () => mode.xtmOne,
}));

const draft = {
  opencti: {
    connector_stale_minutes: 30,
    connector_errors_max: 10,
    queue_depth_max: 100000,
    search_disk_max_percent: 85,
    ingestion_lag_minutes: 15,
  },
  openaev: { integration_silent_minutes: 60, agents_offline_max_percent: 50 },
} as unknown as HealthSettings;

afterEach(() => {
  cleanup();
  mode.xtmOne = true;
});

describe("ModuleThresholdsBlock", () => {
  it("reads every threshold at rest with its unit", () => {
    renderWithProviders(<ModuleThresholdsBlock draft={draft} editing={false} onChange={() => {}} />);
    const cti = screen.getByTestId("settings.health.thresholds.opencti");
    expect(cti).toHaveTextContent("30 minutes");
    expect(cti).toHaveTextContent("100,000 messages");
    expect(cti).toHaveTextContent("85 %");
    const aev = screen.getByTestId("settings.health.thresholds.openaev");
    expect(aev).toHaveTextContent("60 minutes");
    expect(aev).toHaveTextContent("50 %");
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("writes an edited threshold into the draft", () => {
    const onChange = vi.fn();
    renderWithProviders(<ModuleThresholdsBlock draft={draft} editing onChange={onChange} />);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Connector silent after" }), { target: { value: "45" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ opencti: expect.objectContaining({ connector_stale_minutes: 45, queue_depth_max: 100000 }) }));
  });

  it("marks an out-of-bounds value invalid at the field, with its bounds as the error", () => {
    const outOfBounds = { ...draft, opencti: { ...draft.opencti, connector_stale_minutes: 2 } } as HealthSettings;
    renderWithProviders(<ModuleThresholdsBlock draft={outOfBounds} editing onChange={() => {}} />);
    const field = screen.getByRole("spinbutton", { name: "Connector silent after" });
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription("From 5 to 10080 minutes.");
    expect(screen.getByRole("alert")).toHaveTextContent("From 5 to 10080 minutes.");
    expect(screen.getByRole("spinbutton", { name: "Connector errors above" })).toHaveAttribute("aria-invalid", "false");
  });

  it("is absent in Copilot mode", () => {
    mode.xtmOne = false;
    renderWithProviders(<ModuleThresholdsBlock draft={draft} editing={false} onChange={() => {}} />);
    expect(screen.queryByTestId("settings.health.thresholds")).toBeNull();
  });
});

describe("moduleThresholdsValid", () => {
  it("accepts the defaults and refuses a value outside the server's bounds", () => {
    expect(moduleThresholdsValid(draft)).toBe(true);
    expect(moduleThresholdsValid({ ...draft, opencti: { ...draft.opencti, connector_stale_minutes: 4 } })).toBe(false);
    expect(moduleThresholdsValid({ ...draft, openaev: { ...draft.openaev, agents_offline_max_percent: 100 } })).toBe(false);
    expect(moduleThresholdsValid({ ...draft, opencti: { ...draft.opencti, queue_depth_max: Number.NaN } })).toBe(false);
  });
});
