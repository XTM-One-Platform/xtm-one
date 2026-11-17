import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AiLockedFooter, EnterpriseCtaButtons } from "@/components/ui/enterprise-upsell";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { renderWithProviders } from "@/testing/providers";

const viewer = vi.hoisted(() => ({ isAdmin: true, aiOffSentence: null as string | null }));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: viewer.isAdmin }) }));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiDisabledReason: () => viewer.aiOffSentence,
}));

function renderFooter(reason: AiLockReason | null, layout?: "inline" | "stacked") {
  return renderWithProviders(
    <MemoryRouter>
      <AiLockedFooter reason={reason} layout={layout} />
    </MemoryRouter>,
  );
}

describe("AiLockedFooter", () => {
  beforeEach(() => {
    viewer.isAdmin = true;
    viewer.aiOffSentence = null;
  });

  it("renders nothing when AI can run", () => {
    const { container } = renderFooter(null);
    expect(container.querySelector('[data-testid="ai-locked-footer"]')).toBeNull();
  });

  it("gives an administrator in Community Edition the trial and the activation link, never Contact sales", () => {
    renderFooter("unlicensed");
    expect(screen.getByText("AI features run with Enterprise Edition.")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /Start free trial/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Activate License" })).toBeTruthy();
    expect(screen.queryByText("Contact sales")).toBeNull();
  });

  it("gives a non-administrator in Community Edition the sentence alone", () => {
    viewer.isAdmin = false;
    renderFooter("unlicensed");
    expect(screen.getByText("Ask your administrator to activate a license.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("links an administrator with AI off to the AI card of the Platform section", () => {
    viewer.aiOffSentence = "AI is disabled on this platform. An administrator can enable it in Settings → Platform.";
    renderFooter("ai_off");
    expect(screen.getByText(viewer.aiOffSentence)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Turn AI on" }).getAttribute("href")).toBe("/settings/platform#ai-enablement");
  });

  it("keeps the platform sentence when only the backend status knows AI is off", () => {
    viewer.isAdmin = false;
    renderFooter("ai_off");
    expect(screen.getByText(/AI is disabled on this platform/)).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("shows the group sentence and no action, administrator included", () => {
    renderFooter("group_off");
    expect(screen.getByText("AI is disabled for your group. Ask an administrator if you need it.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("sends an administrator without a model to the models settings", () => {
    renderFooter("no_model");
    expect(screen.getByRole("link", { name: "Configure a model" }).getAttribute("href")).toBe("/settings/ai/models");
  });

  it("stacked: keeps the trial and the activation link together in one actions row under the sentence", () => {
    const { container } = renderFooter("unlicensed", "stacked");
    const row = container.querySelector('[data-testid="ai-locked-footer-actions"]') as HTMLElement;
    expect(row).toBeTruthy();
    expect(row.textContent).toContain("Start free trial");
    expect(row.textContent).toContain("Activate License");
    expect(row.textContent).not.toContain("AI features run with Enterprise Edition.");
  });

  it("stacked: draws no actions row for a non-administrator", () => {
    viewer.isAdmin = false;
    const { container } = renderFooter("unlicensed", "stacked");
    expect(container.querySelector('[data-testid="ai-locked-footer-actions"]')).toBeNull();
  });

  // The narrow cards (about 470 px at 1024) keep both actions on one row only
  // stacked: the hero and the stage detail AI assessment card (UX r1 B2, r2 B1).
  // vitest runs from frontend/, as enterprise-cta-guard.test.ts.
  it.each([
    ["xtm-one/CtemCommandCenter.tsx", "aiLockReason"],
    ["xtm-one/CtemStageDetail.tsx", "lockReason"],
  ])("stacked: %s renders its narrow-card footer stacked", (page, reasonVar) => {
    const source = readFileSync(join(process.cwd(), "src", "pages", page), "utf8");
    expect(source).toContain(`<AiLockedFooter reason={${reasonVar}} layout="stacked"`);
  });

  it("tells a non-administrator without a model to ask, in model words", () => {
    viewer.isAdmin = false;
    renderFooter("no_model");
    expect(screen.getByText("No default model is configured. Contact your administrator to configure it.")).toBeTruthy();
    expect(screen.queryByText(/LLM/)).toBeNull();
  });
});

describe("EnterpriseCtaButtons", () => {
  it("renders both Filigran paths by default", () => {
    renderWithProviders(<EnterpriseCtaButtons />);
    expect(screen.getByText("Start free trial")).toBeTruthy();
    expect(screen.getByText("Contact sales")).toBeTruthy();
  });

  it("leaves out Contact sales on request", () => {
    renderWithProviders(<EnterpriseCtaButtons withContactSales={false} />);
    expect(screen.getByText("Start free trial")).toBeTruthy();
    expect(screen.queryByText("Contact sales")).toBeNull();
  });
});
