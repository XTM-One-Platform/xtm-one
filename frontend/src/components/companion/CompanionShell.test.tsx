// The app on a phone, as the person meets it: three tabs, Today as home, and a
// platform page it does not offer saying so instead of being drawn.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/useActivityStream", () => ({ useActivityStream: () => {} }));
vi.mock("@/hooks/useStaleBuildNotice", () => ({ useStaleBuildNotice: () => {} }));
vi.mock("@/hooks/useThemeEffect", () => ({ useThemeEffect: () => {} }));
vi.mock("@/components/layout/EmergencyStopBanner", () => ({ EmergencyStopBanner: () => null }));
const auth = vi.hoisted(() => ({ ctemAccess: "view" as string | null }));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ isImpersonating: false, isAdmin: false, user: { ctem_access: auth.ctemAccess }, logout: vi.fn() }),
}));
vi.mock("@/hooks/api/useAuthApi", () => ({ useMe: () => ({ data: { full_name: "Romain Example" } }) }));

const install = vi.hoisted(() => ({ method: "unavailable" as string, install: vi.fn() }));
vi.mock("@/hooks/useInstallApp", () => ({ useInstallApp: () => install }));

const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api: { get: apiGet } }));
vi.mock("@/hooks/api/useNotificationApi", () => ({
  useNotifications: () => ({ data: { items: [], unread_count: 3, read_at: null } }),
}));
vi.mock("./EnableNotificationsCard", () => ({ EnableNotificationsCard: () => <p>Notifications card</p> }));
// Today draws the platform's home dashboard for its mode.
vi.mock("@/pages/xtm-one/XtmOneDashboard", () => ({ default: () => <p>XTM One home dashboard</p> }));
vi.mock("@/pages/Dashboard", () => ({ default: () => <p>Copilot home dashboard</p> }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  usePlatformConfig: () => ({ platform_mode: "xtm_one" }),
}));

import { CompanionShell } from "./CompanionShell";

function drawAt(path: string) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<CompanionShell />}>
          <Route path="/inbox" element={<p>Inbox page</p>} />
          <Route path="*" element={<p>Platform page</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("CompanionShell", () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiGet.mockImplementation(async () => ({}));
    install.method = "unavailable";
    auth.ctemAccess = "view";
    localStorage.clear();
  });
  afterEach(() => vi.useRealTimers());

  it("offers three tabs, the current one marked, the Inbox with its unread count", () => {
    drawAt("/inbox");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = [...nav.querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href"), a.getAttribute("aria-current")]);
    expect(links).toEqual([
      ["Today", "/", null],
      ["3 unreadInbox", "/inbox", "page"],
      ["Ask", "/chat", null],
    ]);
    expect(screen.getByText("Inbox page")).toBeTruthy();
  });

  it("says a platform page is made for a computer instead of drawing it", () => {
    drawAt("/agents/42");
    expect(screen.getByRole("heading", { name: "This page is made for a computer" })).toBeTruthy();
    expect(screen.queryByText("Platform page")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back to Today" }));
    expect(screen.getByRole("navigation", { name: "Main" }).querySelector('[aria-current="page"]')?.textContent).toBe("Today");
  });

  it("makes Today the home: the platform's home dashboard under the account menu", async () => {
    drawAt("/");
    expect(await screen.findByText("XTM One home dashboard")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Account" })).toBeTruthy();
    expect(screen.getByText("Notifications card")).toBeTruthy();
    expect(screen.queryByText("Platform page")).toBeNull();
  });

  it("gives a person without a CTEM right the mode-neutral home, not the command center (#2441)", async () => {
    auth.ctemAccess = null;
    drawAt("/");
    expect(await screen.findByText("Copilot home dashboard")).toBeTruthy();
    expect(screen.queryByText("XTM One home dashboard")).toBeNull();
  });

  it("offers to install where the browser can, until dismissed", () => {
    install.method = "prompt";
    drawAt("/inbox");
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(install.install).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("region", { name: "Install the app" })).toBeNull();
  });

  it("explains Add to Home Screen on iOS, and offers nothing installed", () => {
    install.method = "ios";
    const { unmount } = drawAt("/inbox");
    expect(screen.getByText(/In Safari, tap Share.*then Add to Home Screen\./)).toBeTruthy();
    unmount();
    install.method = "installed";
    drawAt("/inbox");
    expect(screen.queryByRole("region", { name: "Install the app" })).toBeNull();
  });
});
