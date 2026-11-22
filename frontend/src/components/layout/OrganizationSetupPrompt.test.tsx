import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

/**
 * The first-connection setup proposal (#9173 S3, versioned by #1087): the
 * server decides who sees it; the client asks only as an administrator in XTM
 * One mode, every answer is sent once and closes it, "Set up now" opens the
 * guided CTEM loop setup in place, and the close cross or Escape only closes
 * it for this page: nothing is recorded, so a reload proposes it again.
 */

const auth: { user: { role: string; impersonating?: boolean } | null } = { user: { role: "admin" } };
let xtmOne = true;

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => auth }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useIsXtmOne: () => xtmOne,
}));
// The guided setup has its own tests: here only that "Set up now" opens it.
vi.mock("@/components/fleets/CtemLoopSetupDialog", () => ({
  CtemLoopSetupDialog: () => <div data-testid="ctem-loop-setup" />,
}));

const { OrganizationSetupPrompt } = await import("./OrganizationSetupPrompt");

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

function Where() {
  const location = useLocation();
  return <span data-testid="where">{`${location.pathname}${location.search}`}</span>;
}

function renderPrompt() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/"]}>
      <OrganizationSetupPrompt />
      <Where />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  auth.user = { role: "admin" };
  xtmOne = true;
  get.mockReset().mockResolvedValue({ propose: true, outcome: null, answered_at: null });
  post.mockReset().mockImplementation(async (_url: string, body?: unknown) => ({ propose: false, outcome: (body as { outcome: string }).outcome, answered_at: "2026-11-08T23:00:00Z" }));
});

describe("OrganizationSetupPrompt", () => {
  it("proposes the setup to an administrator when the server says so", async () => {
    renderPrompt();
    expect(await screen.findByRole("dialog", { name: "Set up the CTEM loop" })).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/organization-context/setup-prompt");
  });

  it("shows nothing when the server does not propose it", async () => {
    get.mockResolvedValue({ propose: false, outcome: "dismissed", answered_at: "2026-11-08T23:00:00Z" });
    renderPrompt();
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each([
    ["a user who is not an administrator", () => (auth.user = { role: "user" })],
    ["Copilot mode", () => (xtmOne = false)],
    ["an administrator acting as another user", () => (auth.user = { role: "admin", impersonating: true })],
  ])("never asks for %s", async (_name, arrange) => {
    arrange();
    renderPrompt();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(get).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Set up now records the answer and opens the guided setup in place", async () => {
    renderPrompt();
    fireEvent.click(await screen.findByRole("button", { name: "Set up now" }));
    expect(await screen.findByTestId("ctem-loop-setup")).toBeTruthy();
    expect(post).toHaveBeenCalledWith("/organization-context/setup-prompt", { outcome: "started" });
    expect(screen.getByTestId("where").textContent).toBe("/");
  });

  it.each([
    ["Later", "later"],
    ["Dismiss", "dismissed"],
  ])("%s records the answer once, closes and stays on the page", async (label, outcome) => {
    renderPrompt();
    fireEvent.click(await screen.findByRole("button", { name: label }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith("/organization-context/setup-prompt", { outcome });
    expect(screen.getByTestId("where").textContent).toBe("/");
  });

  it.each([
    ["the close cross", () => fireEvent.click(screen.getByRole("button", { name: "Close" }))],
    ["Escape", () => fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })],
  ])("%s closes for this page without recording an answer, and a reload proposes it again", async (_name, close) => {
    const first = renderPrompt();
    await screen.findByRole("dialog", { name: "Set up the CTEM loop" });
    close();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByTestId("where").textContent).toBe("/");

    first.unmount();
    renderPrompt();
    expect(await screen.findByRole("dialog", { name: "Set up the CTEM loop" })).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
  });

  it("orders the buttons with the secondary choices before the primary one", async () => {
    renderPrompt();
    const dialog = await screen.findByRole("dialog");
    const labels = Array.from(dialog.querySelectorAll("button")).map((button) => button.textContent?.trim()).filter((text) => text);
    expect(labels.slice(-3)).toEqual(["Dismiss", "Later", "Set up now"]);
  });
});
