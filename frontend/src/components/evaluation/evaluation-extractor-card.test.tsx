// The extractor card's pasted-sample preview: a tool source previews the pasted
// text as that tool's last successful call, the shape a run records, so the preview reads
// it as a scored entry would; an agent_output source sends the text as the answer.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { ExtractorSpec } from "@/types";
import { EvaluationExtractorCard, type ExtractorPreviewTarget } from "./evaluation-extractor-card";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const post = vi.mocked(api.post);
const AGENT = { id: "agent-1", name: "Triage" };
const PASTED = '{"cases": [{"id": 1}]}';

function renderCard(source: ExtractorSpec["source"], target: ExtractorPreviewTarget = { evaluationId: "eval-1" }) {
  const extractor: ExtractorSpec = { source, operations: [{ op: "find_json" }], output_kind: "object" };
  renderWithProviders(
    <EvaluationExtractorCard
      target={target}
      agents={[AGENT]}
      extractors={{ [AGENT.id]: extractor }}
      canManage
      onChange={() => {}}
    />,
  );
  return extractor;
}

const OUTPUT_LABEL = "Sample output to preview for Triage";

function paste(text: string, label = OUTPUT_LABEL) {
  fireEvent.change(screen.getByLabelText(label), { target: { value: text } });
}

describe("EvaluationExtractorCard pasted-sample preview", () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({ status: "extracted", prediction: "{}", output_kind: "object", item_count: 1 });
  });

  it.each([
    ["tool_output", "Paste a sample output of search_cases", OUTPUT_LABEL],
    ["tool_input", "Paste a sample input of search_cases", "Sample input to preview for Triage"],
  ] as const)("sends a %s sample as one successful call to the tool", async (kind, placeholder, label) => {
    const extractor = renderCard({ kind, tool_name: "search_cases" });
    expect(screen.getByPlaceholderText(placeholder)).toBeTruthy();

    // The field is named for what it asks for, input or output.
    paste(PASTED, label);
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith("/evaluations/eval-1/extractors/preview", {
      agent_id: AGENT.id,
      extractor,
      result_id: null,
      sample: {
        traces: [{ type: "tool", tool_name: "search_cases", outcome: "success", [kind]: PASTED }],
      },
    });
    expect(await screen.findByText(/Extracted 1 item\b/)).toBeTruthy();
  });

  it("sends an agent_output sample as the answer", async () => {
    renderCard({ kind: "agent_output" });

    paste(PASTED);
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1]).toMatchObject({ sample: { agent_output: PASTED } });
  });

  it("keeps a tool source with no tool named from previewing, and says why", () => {
    renderCard({ kind: "tool_output", tool_name: "  " });
    expect(screen.getByPlaceholderText("Name the tool to preview this source.")).toBeTruthy();

    paste(PASTED);

    const button = screen.getByRole("button", { name: "Preview" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(post).not.toHaveBeenCalled();
  });
});

// The creation screen: no evaluation exists yet, so there is no stored result to
// preview against, and a pasted sample is previewed from the family being written.
describe("EvaluationExtractorCard on the creation screen", () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({ status: "extracted", prediction: "{}", output_kind: "object", item_count: 1 });
  });

  it("previews a pasted sample from the family, without an evaluation", async () => {
    const extractor = renderCard({ kind: "agent_output" }, { family: "STRUCTURED_JSON" });

    paste(PASTED);
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith("/evaluations/extractors/preview", {
      family: "STRUCTURED_JSON",
      extractor,
      sample: { agent_output: PASTED },
    });
  });

  it("offers no stored result to preview against", () => {
    renderCard({ kind: "agent_output" }, { family: "STRUCTURED_JSON" });

    expect(screen.queryByLabelText("Preview source for Triage")).toBeNull();
    expect(screen.getByLabelText(OUTPUT_LABEL)).toBeTruthy();
  });

  it("says no agent is selected yet, not that the evaluation has none", () => {
    renderWithProviders(
      <EvaluationExtractorCard
        target={{ family: "STRUCTURED_JSON" }}
        agents={[]}
        extractors={{}}
        canManage
        onChange={() => {}}
      />,
    );

    expect(screen.getByText("No agents selected yet. Add one to configure extraction.")).toBeTruthy();
  });
});

// The card follows the output kinds the evaluation's family scores, as the
// families endpoint serves them.
describe("EvaluationExtractorCard and the family's output kinds", () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({ status: "extracted", prediction: "[]", output_kind: "label_set", item_count: 0 });
  });

  it("switches an extractor on in a kind the family scores", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <EvaluationExtractorCard
        target={{ family: "CLASSIFICATION" }}
        outputKinds={["label_set"]}
        agents={[AGENT]}
        extractors={{}}
        canManage
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("switch", { name: "Extract before scoring for Triage" }));

    expect(onChange).toHaveBeenCalledWith({ [AGENT.id]: expect.objectContaining({ output_kind: "label_set" }) });
  });

  it("says so when the extractor's kind is one the family does not score", () => {
    const extractor: ExtractorSpec = { source: { kind: "agent_output" }, operations: [{ op: "parse_json" }], output_kind: "object" };
    renderWithProviders(
      <EvaluationExtractorCard
        target={{ family: "CLASSIFICATION" }}
        outputKinds={["label_set"]}
        agents={[AGENT]}
        extractors={{ [AGENT.id]: extractor }}
        canManage
        onChange={() => {}}
      />,
    );

    expect(
      screen.getByText("This evaluation's family does not score this output kind. Choose one it scores, or the evaluation cannot be saved."),
    ).toBeTruthy();
  });

  it("says why the switch is disabled when the family takes no extraction", () => {
    renderWithProviders(
      <EvaluationExtractorCard
        target={{ family: "PROSE" }}
        outputKinds={[]}
        agents={[AGENT]}
        extractors={{}}
        canManage
        onChange={() => {}}
      />,
    );

    expect(screen.getByRole("switch", { name: "Extract before scoring for Triage" })).toBeDisabled();
    expect(screen.getByText("This evaluation's family takes no output extraction in this version.")).toBeTruthy();
    expect(screen.queryByText("Score the raw output")).toBeNull();
  });

  it("drops a preview made under another family", async () => {
    const extractor: ExtractorSpec = { source: { kind: "agent_output" }, operations: [{ op: "parse_json" }], output_kind: "label_set" };
    const card = (family: string) => (
      <EvaluationExtractorCard
        target={{ family }}
        outputKinds={["label_set"]}
        agents={[AGENT]}
        extractors={{ [AGENT.id]: extractor }}
        canManage
        onChange={() => {}}
      />
    );
    const { rerender } = renderWithProviders(card("CLASSIFICATION"));
    paste("[]");
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText(/Extracted 0 items/)).toBeTruthy();

    rerender(card("STRUCTURED_JSON"));

    expect(screen.queryByText(/Extracted 0 items/)).toBeNull();
  });
});
