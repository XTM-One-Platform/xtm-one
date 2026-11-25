/**
 * Observability > Configure, audit log retention field: a value out of the
 * bounds is kept as typed, marked aria-invalid with an inline error named by
 * the field's description, and Save is refused with the focus moved to the
 * field (never clamped, never sent). `yarn test:unit` (vitest + happy-dom).
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const updateObservability = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: true }) }));
vi.mock("@/hooks/api/useSettingsApi", () => ({
  useObservabilityConfig: () => ({
    data: {
      trace_storage_enabled: true,
      trace_retention_days: 90,
      trace_store_payloads: true,
      audit_log_retention_days: 90,
      coordination_retention: {
        work_item_resolved_days: 90,
        work_item_abandoned_days: 14,
        shared_state_resolved_days: 0,
        shared_state_archive_days: 14,
        workset_resolved_days: 7,
      },
      trace_store_provider: "postgresql",
      trace_store_health_status: "healthy",
    },
  }),
  useLangfuseConfig: () => ({ data: { enabled: false, host: "", is_from_env: true } }),
  useUpdateObservabilityConfig: () => ({ mutateAsync: updateObservability, isPending: false }),
  useUpdateLangfuseConfig: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useTestLangfuse: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const { ObservabilitySection } = await import("./ObservabilitySection");

function openConfigure() {
  renderWithProviders(<ObservabilitySection />);
  fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
  return screen.getByRole("spinbutton", { name: "Audit log retention (days)" }) as HTMLInputElement;
}

describe("ObservabilitySection audit log retention field", () => {
  it("keeps an out-of-bounds value as typed, marks it invalid and refuses Save", () => {
    const field = openConfigure();
    expect(field.getAttribute("aria-invalid")).toBeNull();

    fireEvent.change(field, { target: { value: "29" } });
    fireEvent.blur(field);

    expect(field.value).toBe("29");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    const error = screen.getByText("Enter a number of days from 30 to 3650.");
    expect(field.getAttribute("aria-describedby")?.split(" ")).toContain(error.id);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(updateObservability).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(field);
  });

  it("clears the error once the value is back within the bounds", () => {
    const field = openConfigure();
    fireEvent.change(field, { target: { value: "3651" } });
    expect(field.getAttribute("aria-invalid")).toBe("true");

    fireEvent.change(field, { target: { value: "365" } });
    expect(field.getAttribute("aria-invalid")).toBeNull();
    expect(screen.queryByText("Enter a number of days from 30 to 3650.")).toBeNull();
  });

  it("labels each field at body size with its help line tied to the field", () => {
    openConfigure();
    for (const [name, help] of [
      ["Audit log retention (days)", /Audit log entries older than this/],
      ["Trace retention (days)", /Traces older than this are deleted automatically/],
    ] as const) {
      const input = screen.getByRole("spinbutton", { name });
      const hint = screen.getByText(help);
      expect(input.getAttribute("aria-describedby")?.split(" ")).toContain(hint.id);
    }
    for (const name of ["Enable local trace storage", "Store full payloads"]) {
      expect(screen.getByRole("switch", { name })).toBeInTheDocument();
    }
  });

  it("groups the fields under titled groups and gives every field an info bubble", () => {
    openConfigure();
    for (const name of ["Local tracing", "Audit log"]) {
      expect(screen.getByRole("heading", { level: 4, name })).toBeInTheDocument();
    }
    expect(screen.getByText("What the platform stores of each run, and for how long.")).toBeInTheDocument();
    expect(screen.getByText("How long the record of user and administrator actions is kept.")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "About this field" }).length).toBeGreaterThanOrEqual(4);
  });

  it("puts Cancel before Save in the action row", () => {
    openConfigure();
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const save = screen.getByRole("button", { name: "Save" });
    expect(cancel.compareDocumentPosition(save) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("ObservabilitySection trace retention field", () => {
  it("keeps an out-of-bounds value as typed instead of clamping it, and refuses Save", () => {
    openConfigure();
    const field = screen.getByRole("spinbutton", { name: "Trace retention (days)" }) as HTMLInputElement;
    fireEvent.change(field, { target: { value: "400" } });

    expect(field.value).toBe("400");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    const error = screen.getByText("Enter a number of days from 1 to 365.");
    expect(field.getAttribute("aria-describedby")?.split(" ")).toContain(error.id);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(updateObservability).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(field);
  });

  it("lets the field be emptied while typing, without snapping to 1", () => {
    openConfigure();
    const field = screen.getByRole("spinbutton", { name: "Trace retention (days)" }) as HTMLInputElement;
    fireEvent.change(field, { target: { value: "" } });
    expect(field.value).toBe("");
    expect(field.getAttribute("aria-invalid")).toBeNull();
  });
});

describe("ObservabilitySection coordination retention", () => {
  it("reads the stored windows, 0 shown as kept forever", () => {
    renderWithProviders(<ObservabilitySection />);
    const panel = screen.getByTestId("observability-coordination-retention");
    expect(panel.textContent).toContain("90 days");
    expect(panel.textContent).toContain("Kept forever");
  });

  it("marks a value past 3650 invalid and refuses Save with the focus on it", () => {
    updateObservability.mockClear();
    openConfigure();
    const field = screen.getByRole("spinbutton", { name: "Closed work items (days)" }) as HTMLInputElement;
    expect(field.value).toBe("90");
    fireEvent.change(field, { target: { value: "3651" } });
    expect(field.getAttribute("aria-invalid")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(updateObservability).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(field);
  });

  it("sends every window with Save, 0 included", () => {
    updateObservability.mockClear();
    openConfigure();
    const field = screen.getByRole("spinbutton", { name: "Closed work items (days)" });
    fireEvent.change(field, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(updateObservability).toHaveBeenCalledWith(
      expect.objectContaining({
        coordination_retention: {
          work_item_resolved_days: 0,
          shared_state_resolved_days: 0,
          work_item_abandoned_days: 14,
          shared_state_archive_days: 14,
          workset_resolved_days: 7,
        },
      }),
    );
  });
});
