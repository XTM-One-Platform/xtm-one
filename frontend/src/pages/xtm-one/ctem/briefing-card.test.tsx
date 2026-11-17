// Exposure briefing widget: the shared generic notice while no organization
// profile was ever saved (#11324), never on a sample or a locked card.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemBriefing } from "@/types";
import { BriefingCard } from "./BriefingCard";

const ctx = vi.hoisted(() => ({ isAdmin: true, data: { version: null } as { version: number | null } | undefined }));
vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: ctx.isAdmin }),
}));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: ctx.data }),
  useNoOrganizationProfile: () => ctx.data !== undefined && ctx.data.version == null,
}));

const briefing = {
  vertical: "cyber",
  headline: "Two exposed services",
  summary: "Summary",
  posture_trend: "stable",
  key_findings: [],
  next_steps: [],
} as unknown as CtemBriefing;

function card(props: Partial<Parameters<typeof BriefingCard>[0]> = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <BriefingCard briefing={briefing} isSample={false} agentName="Exposure Briefing Analyst" {...props} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  ctx.isAdmin = true;
  ctx.data = { version: null };
});

describe("BriefingCard while a run regenerates the briefing (#12217)", () => {
  beforeEach(() => {
    ctx.data = { version: 2 };
  });

  it("keeps the stored briefing with its byline, Out of date and Generating a new one..., never Sample", () => {
    card({ generatedAt: "2026-11-10T08:00:00Z", stale: true, generating: true });
    expect(screen.getByText("Two exposed services")).toBeTruthy();
    expect(screen.getByTestId("generated-byline")).toHaveTextContent("By Exposure Briefing Analyst");
    expect(screen.getByText("Out of date")).toBeTruthy();
    expect(screen.getByText("Generating a new one...")).toBeTruthy();
    expect(screen.queryByText("Sample")).toBeNull();
  });

  it("keeps the first-run state when nothing is stored yet", () => {
    card({ isSample: true, generating: true });
    expect(screen.getByText("Generating your briefing - scoped to your access")).toBeTruthy();
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(screen.queryByText("Out of date")).toBeNull();
  });

  it("shows a fresh briefing as before", () => {
    card({ generatedAt: "2026-11-16T05:00:00Z", stale: false });
    expect(screen.getByTestId("generated-byline")).toBeTruthy();
    expect(screen.queryByText("Out of date")).toBeNull();
    expect(screen.queryByText("Generating a new one...")).toBeNull();
    expect(screen.queryByText("Sample")).toBeNull();
  });
});

describe("BriefingCard generic notice", () => {
  it("says the briefing is generic with the setup call to action while no profile exists", () => {
    card();
    expect(screen.getByText("This briefing is generic")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Set up organization context" }).getAttribute("href")).toBe("/settings/organization?setup=1");
  });

  it("tells a reader who cannot set it up that an administrator can", () => {
    ctx.isAdmin = false;
    card();
    expect(screen.getByTestId("ctem-briefing-generic")).toHaveTextContent("An administrator can set it up.");
    expect(screen.queryByRole("link", { name: /organization context/ })).toBeNull();
  });

  it("shows nothing once a profile exists, while it loads, on a sample or when locked", () => {
    ctx.data = { version: 2 };
    const a = card();
    expect(screen.queryByText("This briefing is generic")).toBeNull();
    a.unmount();
    ctx.data = undefined;
    const b = card();
    expect(screen.queryByText("This briefing is generic")).toBeNull();
    b.unmount();
    ctx.data = { version: null };
    const c = card({ isSample: true });
    expect(screen.queryByText("This briefing is generic")).toBeNull();
    c.unmount();
    card({ lockReason: "ai_off" });
    expect(screen.queryByText("This briefing is generic")).toBeNull();
  });
});
