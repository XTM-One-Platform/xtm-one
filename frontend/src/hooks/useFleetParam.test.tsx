// A `?fleet=` while the reader's fleet access loads makes the list wait, and
// once access is none it leaves the URL with a replace navigation.

import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigationType, useSearchParams } from "react-router-dom";

import { useFleetParam } from "./useFleetParam";

type Access = "unknown" | "none" | "some";
let access: Access = "unknown";

vi.mock("@/hooks/api/useFleetApi", () => ({
  useAccessibleFleets: () => ({ fleets: [], access }),
}));

function mount(url: string, locked?: string) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={["/start", url]} initialIndex={1}>
      {children}
    </MemoryRouter>
  );
  return renderHook(
    () => ({
      fleet: useFleetParam(locked),
      params: useSearchParams()[0],
      location: useLocation(),
      navigation: useNavigationType(),
    }),
    { wrapper },
  );
}

describe("useFleetParam", () => {
  beforeEach(() => {
    access = "unknown";
  });

  it("waits while access loads and the URL names a fleet", () => {
    const hook = mount("/runs?fleet=f1&q=x");
    const [fleet, , pending] = hook.result.current.fleet;
    expect(pending).toBe(true);
    expect(fleet).toBe("f1");
    expect(hook.result.current.params.get("fleet")).toBe("f1");
  });

  it("does not wait without a URL fleet", () => {
    const hook = mount("/runs?q=x");
    expect(hook.result.current.fleet[2]).toBe(false);
    expect(hook.result.current.fleet[0]).toBeUndefined();
  });

  it("applies the fleet once access is some", () => {
    access = "some";
    const hook = mount("/runs?fleet=f1");
    expect(hook.result.current.fleet[0]).toBe("f1");
    expect(hook.result.current.fleet[2]).toBe(false);
    expect(hook.result.current.params.get("fleet")).toBe("f1");
  });

  it("drops ?fleet= with a replace navigation once access is none", () => {
    access = "none";
    const hook = mount("/runs?fleet=f1&q=x");
    expect(hook.result.current.fleet[0]).toBeUndefined();
    expect(hook.result.current.fleet[2]).toBe(false);
    expect(hook.result.current.params.get("fleet")).toBeNull();
    expect(hook.result.current.params.get("q")).toBe("x");
    expect(hook.result.current.location.search).toBe("?q=x");
    expect(hook.result.current.navigation).toBe("REPLACE");
  });

  it("never waits nor rewrites the URL when locked on a fleet", () => {
    access = "none";
    const hook = mount("/fleets/f9?fleet=f1", "f9");
    expect(hook.result.current.fleet[0]).toBe("f9");
    expect(hook.result.current.fleet[2]).toBe(false);
    expect(hook.result.current.params.get("fleet")).toBe("f1");
  });
});
