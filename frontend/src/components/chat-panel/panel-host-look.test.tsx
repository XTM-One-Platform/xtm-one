// The panel's accent follows the page's, as the page's own controls do: the
// administrator's branding is a <style> the platform config adds once it has
// loaded, often after the panel mounted.

import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { usePlatformLook } from "@/components/chat-panel/panel-host";

function brand(accent: string): HTMLStyleElement {
  const style = document.createElement("style");
  style.textContent = `:root:root{--color-accent:${accent}}`;
  document.head.appendChild(style);
  return style;
}

describe("usePlatformLook", () => {
  afterEach(() => {
    document.head.querySelectorAll("style").forEach((style) => style.remove());
  });

  it("takes the administrator's accent when the branding loads after the panel", async () => {
    brand("#0fbcff");
    const { result } = renderHook(() => usePlatformLook());
    expect(result.current.accentColor).toBe("#0fbcff");

    brand("#e4572e");
    await waitFor(() => expect(result.current.accentColor).toBe("#e4572e"));
  });

  it("reads the accent when it may have changed, not on each render", () => {
    brand("#0fbcff");
    const probes = vi.spyOn(document.body, "appendChild");
    const { result, rerender } = renderHook(() => usePlatformLook());
    const read = probes.mock.calls.length;
    for (let i = 0; i < 5; i += 1) rerender();
    expect(result.current.accentColor).toBe("#0fbcff");
    expect(probes.mock.calls.length, "a re-render laid out the accent probe again").toBe(read);
    probes.mockRestore();
  });

  it("follows a change of the branding's colour", async () => {
    const style = brand("#0fbcff");
    const { result } = renderHook(() => usePlatformLook());
    style.textContent = ":root:root{--color-accent:#2ecc71}";
    await waitFor(() => expect(result.current.accentColor).toBe("#2ecc71"));
  });
});
