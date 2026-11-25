// The log-in-once hint of a stage card (#2042): a member whose newest stage run
// was skipped for want of a module account is told which module to sign in to,
// and the page never auto-generates another run that would be skipped again.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemStageAssessmentResponse } from "@/types";
import { shouldAutoGenerate } from "./assessment-freshness";
import { StageSignInHint } from "./stage-sign-in-hint";

describe("StageSignInHint", () => {
  it("names the modules the member has no account on", () => {
    renderWithProviders(<StageSignInHint modules={["opencti", "openaev"]} />);
    const hint = screen.getByTestId("ctem-stage-sign-in-hint").textContent ?? "";
    expect(hint).toContain("OpenCTI");
    expect(hint).toContain("OpenAEV");
    expect(hint).toContain("once");
  });

  it("names one module alone", () => {
    renderWithProviders(<StageSignInHint modules={["openaev"]} />);
    const hint = screen.getByTestId("ctem-stage-sign-in-hint").textContent ?? "";
    expect(hint).toContain("OpenAEV");
    expect(hint).not.toContain("OpenCTI");
  });

  it("falls back to both modules when the run named none", () => {
    renderWithProviders(<StageSignInHint modules={[]} />);
    expect(screen.getByTestId("ctem-stage-sign-in-hint").textContent).toContain("OpenCTI or OpenAEV");
  });

  it("never auto-generates a stage skipped for a missing account", () => {
    const resp = { assessment: null, status: "no_module_account", no_module_account: ["opencti"] } as CtemStageAssessmentResponse;
    expect(shouldAutoGenerate(resp)).toBe(false);
  });
});

// UX round 1 B1 on #2075: the stage page reads the same endpoint, so it shows
// the same shared hint and offers no Generate that the preflight skips again.
describe("the stage page in the no_module_account state", () => {
  const page = readFileSync(join(process.cwd(), "src/pages/xtm-one/CtemStageDetail.tsx"), "utf8");

  it("renders the shared hint, not a copy", () => {
    expect(page).toContain('from "./ctem/stage-sign-in-hint"');
    expect(page).toContain("<StageSignInHint modules={unsignedModules}");
  });

  it("hides Generate while the member has no module account", () => {
    expect(page).toContain("!generating && !refresh.isPending && !unsignedModules && (");
  });
});
