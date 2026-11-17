// The source picker says whose a built pack is and where it sends the subject.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { SourcePackOption, SubjectPlan } from "@/types";
import { SourcePackPicker } from "./SourcePackPicker";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

function pack(slug: string, extra: Partial<SourcePackOption>): SourcePackOption {
  return {
    slug,
    label: slug,
    description: "",
    subject_types: ["domain"],
    recommended: false,
    applicable_source_count: 1,
    total_source_count: 1,
    applicable_source_names: ["OpenCTI lookup"],
    max_iterations: 10,
    ...extra,
  };
}

function serve(packs: SourcePackOption[], preferredSlugs: string[] | null = null) {
  const plan: SubjectPlan = { subject: "example.com", subject_type: "domain", packs, has_applicable_source: true };
  vi.mocked(api.get).mockResolvedValue(plan);
  renderWithProviders(
    <SourcePackPicker subject="example.com" preferredSlugs={preferredSlugs} onConfirm={() => {}} onCancel={() => {}} />,
  );
}

/** The row's checkbox, found by the pack's name. */
async function row(label: string): Promise<HTMLElement> {
  return (await screen.findByText(label)).closest("[role=checkbox]") as HTMLElement;
}

describe("SourcePackPicker", () => {
  it("names a colleague's pack's owner and hosts, and a link cannot tick it", async () => {
    // As a `?investigate=…&pack=pack:1` link anyone can send arms it.
    serve(
      [
        pack("pack:1", {
          label: "Familiar lookup",
          origin: "shared",
          owner_name: "Mallory",
          destinations: ["collector.example", "example.com.mirror.example"],
        }),
        pack("disinfo-infrastructure", { recommended: true, origin: "builtin" }),
      ],
      ["pack:1"],
    );

    const shared = await row("Familiar lookup");
    expect(shared.getAttribute("aria-checked")).toBe("false");
    expect(shared.textContent).toContain("Shared by Mallory");
    // The whole name, never cut to what fits.
    expect(screen.getByText("Shared by Mallory").className).not.toContain("truncate");
    expect(shared.textContent).toContain("Sends the subject to collector.example, example.com.mirror.example");
    expect((await row("disinfo-infrastructure")).getAttribute("aria-checked")).toBe("true");
  });

  it("ticks the user's own pack a builder link names", async () => {
    serve(
      [
        pack("pack:4", { label: "My method", origin: "mine" }),
        pack("disinfo-infrastructure", { recommended: true, origin: "builtin" }),
      ],
      ["pack:4"],
    );

    expect((await row("My method")).getAttribute("aria-checked")).toBe("true");
    expect((await row("disinfo-infrastructure")).getAttribute("aria-checked")).toBe("false");
  });

  it("says a company pack is the company's, and a pack without free HTTP sources sends nowhere", async () => {
    serve([
      pack("pack:2", { label: "Company method", recommended: true, origin: "company", destinations: ["c.example"] }),
      pack("pack:3", { label: "My method", origin: "mine", destinations: [] }),
    ]);

    const company = await row("Company method");
    expect(company.getAttribute("aria-checked")).toBe("true");
    expect(company.textContent).toContain("Company-managed");
    expect(company.textContent).toContain("Sends the subject to c.example");
    const mine = await row("My method");
    expect(mine.textContent).not.toContain("Shared by");
    expect(mine.textContent).not.toContain("Sends the subject to");
  });

  // What a person typed is shown as typed: never parsed as an ICU message,
  // where `'{x}'` quotes the braces away and `{x}` is a placeholder.
  it.each([
    ["Analyst's {x}"],
    ["Don't '{skip}' this"],
    ["It''s done"],
    ["{n, plural, one {# site} other {# sites}}"],
  ])("renders a built pack's name, step and description as written: %s", async (written) => {
    serve([
      pack("pack:5", {
        label: written,
        origin: "mine",
        options: [
          {
            key: "pivot.enabled",
            label: `Step ${written}`,
            description: `About ${written}`,
            default: "on",
            choices: [
              { value: "on", label: "On", description: "" },
              { value: "off", label: "Off", description: "" },
            ],
          },
        ],
      }),
    ], ["pack:5"]);

    expect((await row(written)).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText(`Step ${written}`).textContent).toBe(`Step ${written}`);
    expect(screen.getByText(`About ${written}`).textContent).toBe(`About ${written}`);
    expect(screen.getByRole("radiogroup").getAttribute("aria-label")).toBe(`Step ${written}`);
  });
});
