// B1 of the #9413 UX review: with AI locked and no deterministic rule firing,
// the Recommended actions card explains itself and gives one way forward,
// never an empty body.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { RecommendedActionsEmpty } from "./RecommendedActionsEmpty";

const viewer = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: viewer.isAdmin }) }));

function renderEmpty(lockReason: AiLockReason | null, isAdmin = true) {
  viewer.isAdmin = isAdmin;
  return renderWithProviders(
    <MemoryRouter>
      <RecommendedActionsEmpty lockReason={lockReason} />
    </MemoryRouter>,
  );
}

describe("RecommendedActionsEmpty", () => {
  it("gives an administrator in Community Edition the empty state and the trial", () => {
    renderEmpty("unlicensed");
    expect(screen.getByText("No recommended action yet")).toBeTruthy();
    expect(screen.getByText("Actions drawn from your exposure posture show here when one applies.")).toBeTruthy();
    expect(screen.getByText("Start free trial")).toBeTruthy();
  });

  it("gives a non-administrator the sentence alone", () => {
    renderEmpty("unlicensed", false);
    expect(screen.getByText("No recommended action yet")).toBeTruthy();
    expect(screen.getByText("Ask your administrator to activate a license.")).toBeTruthy();
    expect(screen.queryByText("Start free trial")).toBeNull();
  });

  it("says AI off first when the platform's AI is off", () => {
    renderEmpty("ai_off");
    expect(screen.getByRole("link", { name: "Turn AI on" })).toBeTruthy();
  });
});
