// The Intents page: a failed read stays in the table (with Retry) instead of taking the page down,
// and a row's bindings open from the keyboard.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), postForm: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import Intents from "./Intents";

const INTENT = {
  id: "i1",
  name: "cti.container_summary",
  description: "Summarize an OpenCTI container",
  platforms: ["opencti", "openaev", "opencrq"],
  bindings: [],
  is_builtin: true,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
};

function draw() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/intents"]}>
      <Intents />
    </MemoryRouter>,
  );
}

describe("Intents", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
    window.localStorage.clear();
  });

  it("keeps the page and shows Retry in the table when the intents read fails", async () => {
    api.get.mockImplementation(async (path: string) => {
      if (path.startsWith("/intents")) throw new Error("Request failed with status 500");
      if (path === "/auth/me") return { id: "u1", role: "admin" };
      return [];
    });
    draw();
    expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Intents" })).toBeTruthy();
  });

  it("opens the bindings from the row, its one tab stop, on one line with a +N platform chip", async () => {
    api.get.mockImplementation(async (path: string) => {
      if (path.startsWith("/intents")) return [INTENT];
      if (path === "/auth/me") return { id: "u1", role: "admin" };
      return [];
    });
    draw();
    const row = await screen.findByTestId("intent-row");
    expect(screen.getByText("+2")).toBeTruthy();
    expect(screen.queryByText("Summarize an OpenCTI container")).toBeNull();
    // The name is plain text: no button in the cell, the row is the one stop (UX round 2 A1).
    expect(screen.getByTestId("intent-name").closest("button")).toBeNull();
    expect(row.getAttribute("tabindex")).toBe("0");
    row.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });
});
