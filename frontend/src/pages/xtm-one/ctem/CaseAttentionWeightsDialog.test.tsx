// SPDX-License-Identifier: Apache-2.0

import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/testing/providers";
import { CaseAttentionWeightsDialog, WEIGHT_FIELDS, WEIGHT_GROUPS, parseWeight } from "./CaseAttentionWeightsDialog";
import { DEFAULT_CASE_ATTENTION_WEIGHTS, weightsFromProfile, weightsToProfile } from "./case-attention";

const saved = vi.hoisted(() => ({ calls: [] as unknown[] }));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { version: 3, profile: { name: "Acme", primary_assets: [] } } }),
  usePatchOrganizationContext: () => ({ isPending: false, mutate: (body: unknown) => saved.calls.push(body) }),
}));

describe("case attention points (#1176 slice 2b)", () => {
  it("maps the profile to the weights and back", () => {
    expect(weightsFromProfile(undefined)).toBe(DEFAULT_CASE_ATTENTION_WEIGHTS);
    const profile = weightsToProfile(DEFAULT_CASE_ATTENTION_WEIGHTS);
    expect(weightsFromProfile(profile)).toEqual(DEFAULT_CASE_ATTENTION_WEIGHTS);
    expect(profile.priority_critical).toBe(40);
  });

  it("takes whole numbers within the bounds only", () => {
    expect(parseWeight("12", 0, 100)).toBe(12);
    expect(parseWeight("101", 0, 100)).toBeNull();
    expect(parseWeight("0", 1, 90)).toBeNull();
    expect(parseWeight("1.5", 0, 100)).toBeNull();
    expect(parseWeight("", 0, 100)).toBeNull();
  });

  it("names a field for every point of the score", () => {
    expect(WEIGHT_FIELDS.map((f) => f.key).sort()).toEqual(Object.keys(weightsToProfile(DEFAULT_CASE_ATTENTION_WEIGHTS)).sort());
  });

  it("puts every field in exactly one titled group (UX r1 B2)", () => {
    const grouped = WEIGHT_GROUPS.flatMap((g) => [...g.keys]);
    expect(grouped.slice().sort()).toEqual(WEIGHT_FIELDS.map((f) => f.key).sort());
    expect(new Set(grouped).size).toBe(grouped.length);
    renderWithProviders(<CaseAttentionWeightsDialog open onClose={() => undefined} />);
    for (const g of WEIGHT_GROUPS) {
      const section = screen.getByTestId(`ctem-attention-group-${g.id}`);
      expect(section.textContent).toContain(g.title);
      expect(section.textContent).toContain(g.description);
    }
  });

  it("gives every field an info text with what it adds (UX r1 B1)", () => {
    for (const f of WEIGHT_FIELDS) {
      expect(f.info.length).toBeGreaterThan(f.helper.length);
    }
  });

  it("saves the points alone, no other field and no asset row (#1752)", () => {
    renderWithProviders(<CaseAttentionWeightsDialog open onClose={() => undefined} />);
    fireEvent.change(screen.getByTestId("ctem-attention-kev-input"), { target: { value: "30" } });
    fireEvent.click(screen.getByTestId("ctem-cases-attention-weights-save"));
    const body = saved.calls[saved.calls.length - 1] as { fields: { case_attention: { kev: number; priority_critical: number } }; based_on_version: number };
    expect(Object.keys(body.fields)).toEqual(["case_attention"]);
    expect(body.based_on_version).toBe(3);
    expect(body.fields.case_attention.kev).toBe(30);
    expect(body.fields.case_attention.priority_critical).toBe(40);
  });

  it("refuses to save an out-of-range value", () => {
    renderWithProviders(<CaseAttentionWeightsDialog open onClose={() => undefined} />);
    fireEvent.change(screen.getByTestId("ctem-attention-due_soon_days-input"), { target: { value: "0" } });
    expect((screen.getByTestId("ctem-cases-attention-weights-save") as HTMLButtonElement).disabled).toBe(true);
  });
});
