// The STIX bundle review (#12282) as a reviewer meets it: one answer checked against the
// expected bundle, two answers compared, the five views, the inspector, and the states of a
// refused review, an unreadable answer and an entry without report text.

import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { BundleReview, BundleReviewAnswer } from "@/types";
import EvaluationBundleReview from "./EvaluationBundleReview";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

const get = vi.mocked(api.get);

function answer(key: string, model: string, entity: number, relationship: number): BundleReviewAnswer {
  return {
    key,
    result_id: key,
    run_id: `run-${key}`,
    evaluation_id: "ev-1",
    evaluation_name: "STIX extraction",
    model_id: model,
    subject_id: null,
    subject_name: null,
    created_at: null,
    judge_score: 70,
    anomaly: null,
    metrics: { "stix.entity_prf": entity, "stix.relationship_prf": relationship },
  };
}

// APT29 (alias Cozy Bear) uses Cobalt Strike and Phishing. Answer r1 names the actor
// Cozy Bear and adds a spurious domain; answer r2 names it right.
function review(over: Partial<BundleReview> = {}): BundleReview {
  return {
    entry: {
      label: "APT29 phishing report",
      pages: ["APT29, also known as Cozy Bear, used Cobalt Strike after Phishing (T1566) from evil.com."],
      file_name: null,
      text_source: "prompt",
    },
    answers: [answer("r1", "gpt-a", 33.3, 0), answer("r2", "gpt-b", 85.7, 66.7)],
    objects: [
      { t: "intrusion-set", n: "APT29", c: "intrusion-set--1", a: ["Cozy Bear"] },
      { t: "tool", n: "Cobalt Strike", c: "tool--1" },
      { t: "attack-pattern", n: "Phishing", c: "attack-pattern--1", x: "T1566" },
      { t: "intrusion-set", n: "Cozy Bear", c: "intrusion-set--2" },
      { t: "domain-name", n: "evil[.]com", c: "domain-name--1" },
    ],
    groups: [0, 1, 2, 0, 3],
    sources: { expected: [0, 1, 2], r1: [1, 3, 4], r2: [0, 1, 2] },
    relationships: [
      [0, 1, { expected: ["uses", 0, 1, 0], r1: ["uses", 3, 1, 0], r2: ["uses", 0, 1, 0] }],
      [0, 2, { expected: ["uses", 0, 2, 0], r2: ["uses", 0, 2, 0] }],
    ],
    unreadable: [],
    ...over,
  };
}

function renderAt(search: string) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[`/evaluations/review${search}`]}>
      <EvaluationBundleReview />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  get.mockReset();
});

describe("EvaluationBundleReview", () => {
  it("checks one answer against the expected bundle and explains a near-miss", async () => {
    const data = review({ answers: [answer("r1", "gpt-a", 33.3, 0)], sources: { expected: [0, 1, 2], r1: [1, 3, 4] } });
    get.mockResolvedValue(data);
    renderAt("?results=r1");

    expect(await screen.findByText("APT29 phishing report")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/evaluations/bundle-review?result_ids=r1");
    expect(screen.getByText("33.3")).toBeInTheDocument();
    expect(screen.getByText(/checked against the expected bundle\./)).toBeInTheDocument();
    // The near-miss and the spurious domain, counted as the scorer counts them.
    expect(screen.getByRole("list", { name: "Objects by status" })).toHaveTextContent("1matched");

    await userEvent.click(screen.getByRole("button", { name: "APT29" }));
    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    expect(within(inspector).getByText(/Near-miss · Alias/)).toBeInTheDocument();
    expect(within(inspector).getByText(/an alias of the expected “APT29”/)).toBeInTheDocument();
  });

  it("compares two answers: what the second fixed", async () => {
    get.mockResolvedValue(review());
    renderAt("?results=r1,r2");

    expect(await screen.findByText(/What changed from A to B/)).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/evaluations/bundle-review?result_ids=r1%2Cr2");
    const filters = screen.getByRole("group", { name: "Status" });
    for (const name of ["Fixed", "Regressed", "New error", "Still wrong", "Unchanged"]) {
      expect(within(filters).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("list", { name: "Objects by status" })).toHaveTextContent("3fixed");
    expect(screen.getByText("85.7")).toBeInTheDocument();
  });

  it("draws the five views and filters them by type", async () => {
    get.mockResolvedValue(review());
    renderAt("?results=r1,r2");
    await screen.findByText(/What changed from A to B/);

    await userEvent.click(screen.getByRole("tab", { name: "Diff" }));
    expect(screen.getByRole("region", { name: "intrusion-set" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Annotate" }));
    expect(screen.getByRole("region", { name: "Page 1 of 1" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /intrusion-set APT29, Fixed, located by/ }).length).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole("tab", { name: "Graph" }));
    expect(screen.getByRole("group", { name: "Merged graph of the compared bundles" })).toBeInTheDocument();
    // A merged node carries the reference's name, the answer's other name under it.
    expect(screen.getByRole("button", { name: "intrusion-set Cozy Bear, Fixed" })).toBeInTheDocument();
    expect(screen.getByText("≈ APT29")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Knowledge graph" }));
    expect(screen.getByRole("group", { name: "Knowledge graph of the compared bundles" })).toBeInTheDocument();
    const node = screen.getByRole("button", { name: "tool Cobalt Strike, Unchanged" });
    node.focus();
    await userEvent.keyboard("{Enter}");
    expect(within(screen.getByRole("complementary", { name: "Inspector" })).getByRole("heading", { name: "Cobalt Strike" })).toBeInTheDocument();

    const rail = screen.getByRole("navigation", { name: "Filters" });
    await userEvent.click(within(rail).getByRole("button", { name: /^attack-pattern/ }));
    expect(screen.getByText("Filtered on type attack-pattern.")).toBeInTheDocument();
  });

  it("says when an entry has no report text", async () => {
    get.mockResolvedValue(review({ entry: { label: "No text", pages: [], file_name: null, text_source: null } }));
    renderAt("?results=r1,r2");
    await screen.findByText(/What changed from A to B/);
    await userEvent.click(screen.getByRole("tab", { name: "Annotate" }));
    expect(screen.getByText("No report text")).toBeInTheDocument();
  });

  it("says when an answer is not a readable bundle", async () => {
    get.mockResolvedValue(review({ unreadable: ["r2"], sources: { expected: [0, 1, 2], r1: [1, 3, 4] } }));
    renderAt("?results=r1,r2");
    expect(await screen.findByText("Answer B is not a readable STIX bundle")).toBeInTheDocument();
  });

  it("shows why the review was refused", async () => {
    get.mockRejectedValue(Object.assign(new Error("Only STIX_BUNDLE answers have a bundle review"), { status: 422 }));
    renderAt("?results=r1");
    expect(await screen.findByText("This review cannot be shown")).toBeInTheDocument();
    expect(screen.getByText("Only STIX_BUNDLE answers have a bundle review")).toBeInTheDocument();
  });

  it("asks for an answer when the link names none", async () => {
    renderAt("");
    expect(await screen.findByText("No answer to review")).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
