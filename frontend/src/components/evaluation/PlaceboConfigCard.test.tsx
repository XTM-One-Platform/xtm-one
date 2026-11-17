// The Configuration tab's placebo control: what it shows, what a save sends, what stops it.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationPlaceboConfig } from "@/types";
import { PlaceboConfigCard } from "./PlaceboConfigCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(((path: string) =>
    Promise.resolve(path.startsWith("/skills") ? [{ id: "s-1", name: "Triage Checklist" }] : [])) as typeof api.get);
  put.mockResolvedValue({});
});

function renderCard(executionMode: string, placeboConfig: EvaluationPlaceboConfig | null) {
  return renderWithProviders(
    <PlaceboConfigCard evaluation={{ id: "e-1", execution_mode: executionMode, placebo_config: placeboConfig }} canManage />,
  );
}

describe("PlaceboConfigCard", () => {
  it("says it is not set up, and a first save sends the default configuration alone", async () => {
    renderCard("full_loop", null);

    expect(screen.getByText(/^Not set up\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Set up" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]).toEqual([
      "/evaluations/e-1",
      {
        placebo_config: {
          targets: [{ kind: "persona", skill_id: null }],
          include_no_target: true,
          trials_per_entry: 5,
          custom_text: null,
        },
      },
    ]);
  });

  it("shows a stored configuration with its single skill by name", async () => {
    renderCard("full_loop", {
      targets: [
        { kind: "prompts", skill_id: null },
        { kind: "skill", skill_id: "s-1" },
      ],
      include_no_target: false,
      trials_per_entry: 12,
      custom_text: null,
    });

    expect(await screen.findByText("Skill · Triage Checklist")).toBeInTheDocument();
    expect(screen.getByText("Prompts")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expect(screen.getByText("The neutral corpus shipped with the platform")).toBeInTheDocument();
  });

  it("keeps Prompts and skills targets out of reach in LLM Only", () => {
    renderCard("llm_only", null);

    fireEvent.click(screen.getByRole("button", { name: "Set up" }));

    expect(screen.getByRole("switch", { name: "Attached Prompts" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "All skills" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Persona" })).toBeEnabled();
  });

  it("names the rule a draft breaks and does not save it", () => {
    renderCard("full_loop", null);

    fireEvent.click(screen.getByRole("button", { name: "Set up" }));
    fireEvent.click(screen.getByRole("switch", { name: "Persona" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Choose at least one target.");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    fireEvent.click(screen.getByRole("switch", { name: "Persona" }));
    fireEvent.change(screen.getByLabelText("Trials per entry"), { target: { value: "31" } });

    expect(screen.getByRole("alert")).toHaveTextContent("Trials per entry must be a whole number between 1 and 30.");
    expect(put).not.toHaveBeenCalled();
  });

  it("removes the configuration with null once confirmed", async () => {
    renderCard("full_loop", {
      targets: [{ kind: "persona", skill_id: null }],
      include_no_target: true,
      trials_per_entry: 5,
      custom_text: null,
    });

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    // The confirmation dialog hides the page behind it: its own "Remove" is the one left.
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]).toEqual(["/evaluations/e-1", { placebo_config: null }]);
  });
});
