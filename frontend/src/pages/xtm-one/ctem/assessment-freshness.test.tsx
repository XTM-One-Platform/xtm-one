// Stage assessments are generated on visit when missing or out of date, dated,
// and carry the follow-up and assign actions on the stage page (#12002, the
// regression of e3685252: the stage page never generated one).

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { renderWithProviders } from "@/testing/providers";
import type { CtemStageAssessmentResponse } from "@/types";
import { AssessmentFreshness, shouldAutoGenerate } from "./assessment-freshness";

function resp(over: Partial<CtemStageAssessmentResponse>): CtemStageAssessmentResponse {
  return { assessment: null, status: "pending", ...over };
}

describe("shouldAutoGenerate", () => {
  it("generates when nothing is stored for the vertical", () => {
    expect(shouldAutoGenerate(resp({ status: "pending" }))).toBe(true);
  });

  it("generates when the stored assessment is out of date", () => {
    expect(shouldAutoGenerate(resp({ status: "ready", stale: true }))).toBe(true);
  });

  it("does not generate a fresh assessment", () => {
    expect(shouldAutoGenerate(resp({ status: "ready", stale: false }))).toBe(false);
  });

  it("never starts a second run while one is in flight", () => {
    expect(shouldAutoGenerate(resp({ status: "generating" }))).toBe(false);
    expect(shouldAutoGenerate(resp({ status: "ready", stale: true, generating: true }))).toBe(false);
  });

  it("never generates on a locked or unknown state", () => {
    for (const status of ["ee_locked", "ai_disabled", "no_model", "unavailable"] as const) {
      expect(shouldAutoGenerate(resp({ status }))).toBe(false);
    }
    expect(shouldAutoGenerate(undefined)).toBe(false);
  });
});

describe("AssessmentFreshness", () => {
  it("names the agent and the age with the shared byline, the date on hover", () => {
    renderWithProviders(<AssessmentFreshness generatedAt="2026-11-14T07:02:00Z" agentName="CTEM Discovery Analyst" />);
    const byline = screen.getByTestId("generated-byline");
    expect(byline.textContent).toMatch(/^By CTEM Discovery Analyst - /);
    expect(byline.getAttribute("aria-label")).toMatch(/Generated /);
    expect(screen.queryByText("Out of date")).toBeNull();
    expect(screen.queryByTestId("assessment-regenerating")).toBeNull();
  });

  it("keeps Out of date and says a new one is generating while a run replaces it", () => {
    renderWithProviders(
      <AssessmentFreshness generatedAt="2026-11-01T07:02:00Z" agentName="CTEM Discovery Analyst" stale generating />,
    );
    expect(screen.getByText("Out of date")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("Generating a new one...");
  });

  it("marks an assessment past the window as out of date", () => {
    renderWithProviders(<AssessmentFreshness generatedAt="2026-11-01T07:02:00Z" agentName="CTEM Discovery Analyst" stale />);
    expect(screen.getByText("Out of date")).toBeTruthy();
  });

  it("renders nothing without a date", () => {
    renderWithProviders(<AssessmentFreshness generatedAt={null} agentName="CTEM Discovery Analyst" />);
    expect(screen.queryByTestId("assessment-freshness")).toBeNull();
  });
});

describe("the stage page", () => {
  const page = readFileSync(join(process.cwd(), "src/pages/xtm-one/CtemStageDetail.tsx"), "utf8");

  it("generates the assessment on visit (the regression of the command center v2 layout)", () => {
    expect(page).toContain("shouldAutoGenerate(assessmentResp)");
    expect(page).toContain("generate({ stage, vertical, auto: true })");
  });

  it("passes the in-flight run to the byline, never hiding Out of date while regenerating", () => {
    expect(page).toContain("generating={generating || refresh.isPending}");
    expect(page).not.toContain("stale && !generating");
  });

  it("dates every assessment and offers both actions outside the recommended action", () => {
    expect(page).toContain("<AssessmentFreshness");
    const actions = page.indexOf("<AssignAgentButton />");
    const recommended = page.indexOf("{assessment.recommended_action && (");
    expect(actions).toBeGreaterThan(recommended);
    expect(page.slice(recommended, actions)).toContain("{/* Every assessment carries both actions");
  });
});

describe("one byline for every CTEM briefing and assessment", () => {
  const files = ["src/pages/xtm-one/ExposureBriefing.tsx", "src/pages/xtm-one/ctem/BriefingCard.tsx"];

  it.each(files)("%s draws the byline through GeneratedByline, never its own string", (file) => {
    const source = readFileSync(join(process.cwd(), file), "utf8");
    expect(source).toContain("<GeneratedByline");
    expect(source).not.toContain('"By {name} - {timeAgo}"');
    expect(source).not.toContain('"By {name}"');
  });
});
