// The companion's PIN lock: on a phone nothing mounts until the PIN is
// entered; on a computer the gate is not there.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";

import { QueryClient } from "@tanstack/react-query";

import { useMe } from "@/hooks/api/useAuthApi";
import { APP_LOCKED_EVENT } from "@/lib/app-lock";
import { renderWithProviders } from "@/testing/providers";
import type { AppLockStatus } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));
const device = vi.hoisted(() => ({ phone: true }));
vi.mock("@/hooks/useCompanionDevice", () => ({ useCompanionDevice: () => device.phone }));

import { AppLockGate } from "./AppLockGate";

function status(overrides: Partial<AppLockStatus> = {}): AppLockStatus {
  return {
    applies: true,
    required: false,
    has_pin: true,
    enabled: true,
    companion: true,
    locked: true,
    setup_required: false,
    attempts_left: 5,
    min_digits: 6,
    max_digits: 8,
    ...overrides,
  };
}

function Profile() {
  const { data } = useMe();
  return <p>{data ? `Hello ${data.full_name}` : "Loading"}</p>;
}

function draw() {
  return renderWithProviders(
    <AppLockGate>
      <p>Today</p>
    </AppLockGate>,
  );
}

function typePin(pin: string) {
  for (const digit of pin) fireEvent.click(screen.getByRole("button", { name: digit }));
}

function setVisibility(state: "hidden" | "visible") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("AppLockGate", () => {
  beforeEach(() => {
    device.phone = true;
    for (const fn of Object.values(api)) fn.mockReset();
    api.get.mockImplementation(async (path: string) => (path === "/app-lock" ? status() : {}));
    api.post.mockImplementation(async (path: string) => {
      if (path === "/app-lock/session") return status();
      if (path === "/app-lock/lock") return status();
      throw new Error(`unexpected POST ${path}`);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  });

  it("is not there on a computer", async () => {
    device.phone = false;
    draw();
    expect(screen.getByText("Today")).toBeTruthy();
    expect(api.post).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("opens locked on a phone and shows the app only once the PIN is right", async () => {
    api.post.mockImplementation(async (path: string, body?: { pin: string }) => {
      if (path === "/app-lock/session") return status();
      if (path === "/app-lock/unlock" && body?.pin === "482915") return status({ locked: false });
      throw Object.assign(new Error("Forbidden"), {
        status: 403,
        detail: { code: "app_pin_wrong", attempts_left: 4 },
      });
    });
    draw();
    expect(await screen.findByText("Enter your PIN")).toBeTruthy();
    expect(screen.queryByText("Today")).toBeNull();
    expect(api.post.mock.calls).toEqual([["/app-lock/session"]]);

    const unlock = screen.getByRole("button", { name: "Unlock" }) as HTMLButtonElement;
    typePin("99911");
    expect(unlock.disabled).toBe(true);
    typePin("1");
    fireEvent.click(unlock);
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "Wrong PIN. 4 attempts left before you are signed out.",
    );
    expect(screen.getByRole("status").getAttribute("aria-label")).toBe("PIN: 0 digits entered");

    typePin("482915");
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByText("Today")).toBeTruthy();
    expect(api.post.mock.calls.filter(([path]) => path === "/app-lock/unlock").map(([, body]) => body)).toEqual([
      { pin: "999111" },
      { pin: "482915" },
    ]);
  });

  it("takes the screen back when a request is refused as locked", async () => {
    api.post.mockImplementation(async () => status({ locked: false }));
    api.get.mockImplementation(async () => status({ locked: true }));
    draw();
    expect(await screen.findByText("Today")).toBeTruthy();
    act(() => {
      window.dispatchEvent(new CustomEvent(APP_LOCKED_EVENT, { detail: { code: "app_locked" } }));
    });
    expect(await screen.findByText("Enter your PIN")).toBeTruthy();
    expect(screen.queryByText("Today")).toBeNull();
  });

  it("locks itself after half a minute out of sight", async () => {
    api.post.mockImplementation(async (path: string) =>
      status({ locked: path === "/app-lock/lock" }),
    );
    draw();
    expect(await screen.findByText("Today")).toBeTruthy();
    const now = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(now);
    act(() => setVisibility("hidden"));
    clock.mockReturnValue(now + 10_000);
    act(() => setVisibility("visible"));
    expect(screen.getByText("Today")).toBeTruthy();

    act(() => setVisibility("hidden"));
    clock.mockReturnValue(now + 10_000 + 30_000);
    act(() => setVisibility("visible"));
    expect(await screen.findByText("Enter your PIN")).toBeTruthy();
    expect(api.post).toHaveBeenCalledWith("/app-lock/lock", undefined, { keepalive: true });
  });

  it("makes the person choose a PIN, twice the same, when the organization requires one", async () => {
    api.post.mockImplementation(async () => status({ required: true, has_pin: false, enabled: false, setup_required: true }));
    api.put.mockImplementation(async () => status({ required: true, locked: false }));
    draw();
    expect(await screen.findByText("Choose a PIN")).toBeTruthy();
    expect(screen.queryByText("Today")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();

    typePin("123456");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Choose a PIN that is not a run of consecutive digits.",
    );

    for (let i = 0; i < 6; i += 1) fireEvent.click(screen.getByRole("button", { name: "Delete the last digit" }));
    typePin("482915");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("Enter the same PIN again")).toBeTruthy();
    typePin("482916");
    fireEvent.click(screen.getByRole("button", { name: "Set PIN" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "The two PINs are not the same. Choose your PIN again.",
    );
    expect(api.put).not.toHaveBeenCalled();

    typePin("482915");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    typePin("482915");
    fireEvent.click(screen.getByRole("button", { name: "Set PIN" }));
    expect(await screen.findByText("Today")).toBeTruthy();
    expect(api.put.mock.calls).toEqual([["/app-lock/pin", { pin: "482915" }]]);
  });

  it("holds its screen while the profile is refused, and reads it again once unlocked", async () => {
    // The app's own client: a refused query throws to the nearest boundary.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
    let locked = true;
    api.get.mockImplementation(async (path: string) => {
      if (path === "/auth/me" && locked) throw Object.assign(new Error("Locked"), { status: 423 });
      return path === "/auth/me" ? { full_name: "Owner" } : status({ locked });
    });
    api.post.mockImplementation(async (path: string) => {
      if (path === "/app-lock/unlock") locked = false;
      return status({ locked });
    });
    // What reads the profile above the gate (the language, the arcade) fails first.
    await client.prefetchQuery({ queryKey: ["me"], queryFn: () => api.get("/auth/me") });
    expect(client.getQueryState(["me"])?.status).toBe("error");
    renderWithProviders(
      <AppLockGate>
        <Profile />
      </AppLockGate>,
      client,
    );
    expect(await screen.findByText("Enter your PIN")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
    typePin("482915");
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByText("Hello Owner")).toBeTruthy();
  });

  it("types with a keyboard too", async () => {
    api.post.mockImplementation(async (path: string) => status({ locked: path !== "/app-lock/unlock" }));
    draw();
    expect(await screen.findByText("Enter your PIN")).toBeTruthy();
    for (const key of "4829150") fireEvent.keyDown(window, { key });
    fireEvent.keyDown(window, { key: "Backspace" });
    fireEvent.keyDown(window, { key: "Enter" });
    expect(await screen.findByText("Today")).toBeTruthy();
    expect(api.post).toHaveBeenCalledWith("/app-lock/unlock", { pin: "482915" });
  });
});
