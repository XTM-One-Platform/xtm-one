/**
 * The stale-build notice - `yarn test:dom`. The page booted build
 * `b1`; `/auth/config`, refetched when the tab comes back past its
 * `staleTime`, reports the server's build. `fetch` and the clock are faked:
 * the query and the toaster are the real ones, the toast call only counted.
 */
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { IntlProvider } from "react-intl";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui/toast";
import { BUILD_ID_META_NAME } from "@/lib/build-version";
import { renderWithProviders, testQueryClient } from "@/testing/providers";

const offered = vi.hoisted(() => [] as string[]);

vi.mock("@/components/ui/toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ui/toast")>();
  const { useMemo } = await import("react");
  return {
    ...actual,
    useToast: () => {
      const { toast } = actual.useToast();
      return useMemo(
        () => ({
          toast: (opts: Parameters<typeof toast>[0]) => {
            offered.push(opts.title ?? "");
            toast(opts);
          },
        }),
        [toast],
      );
    },
  };
});

const TITLE = "A new version is available";
/** `/auth/config`'s `staleTime`, and a little more. */
const AWAY_MS = 61_000;

let useStaleBuildNotice: typeof import("./useStaleBuildNotice").useStaleBuildNotice;
let serverBuild = "b1";
let configFetches = 0;

beforeAll(async () => {
  // The page's build is read once, when the module loads.
  const meta = document.createElement("meta");
  meta.name = BUILD_ID_META_NAME;
  meta.content = "b1";
  document.head.appendChild(meta);
  ({ useStaleBuildNotice } = await import("./useStaleBuildNotice"));
});

beforeEach(() => {
  offered.length = 0;
  serverBuild = "b1";
  configFetches = 0;
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    if (!String(input).endsWith("/api/v1/auth/config")) return new Response("{}", { status: 404 });
    configFetches += 1;
    return new Response(JSON.stringify({ build_id: serverBuild }), { headers: { "Content-Type": "application/json" } });
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function Page() {
  useStaleBuildNotice();
  return null;
}

async function openPage() {
  renderWithProviders(<Page />);
  await waitFor(() => expect(configFetches).toBe(1));
}

/** The tab is left past the config's `staleTime`, then comes back: the config is refetched. */
async function returnToTab() {
  const before = configFetches;
  vi.setSystemTime(Date.now() + AWAY_MS);
  act(() => {
    window.dispatchEvent(new Event("visibilitychange"));
  });
  await waitFor(() => expect(configFetches).toBe(before + 1));
  // The refetched data reaches the hook, and its effect runs.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("useStaleBuildNotice", () => {
  it("offers nothing while the server serves the page's build", async () => {
    await openPage();
    await returnToTab();
    expect(offered).toEqual([]);
    expect(screen.queryByText(TITLE)).toBeNull();
  });

  it("offers a reload once per server build, on coming back to the tab", async () => {
    await openPage();

    serverBuild = "b2";
    await returnToTab();
    expect(offered).toEqual([TITLE]);
    expect(await screen.findByText(TITLE)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();

    // Still b2: not offered again, on screen or closed.
    await returnToTab();
    expect(offered).toEqual([TITLE]);
    fireEvent.click(screen.getByRole("button", { name: "Close notification" }));
    await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());
    await returnToTab();
    expect(offered).toEqual([TITLE]);
    expect(screen.queryByText(TITLE)).toBeNull();

    // A later build is offered anew.
    serverBuild = "b3";
    await returnToTab();
    expect(offered).toEqual([TITLE, TITLE]);
    expect(await screen.findByText(TITLE)).toBeTruthy();
  });

  it("is not offered again for the same build when the page re-renders its strings", async () => {
    const client = testQueryClient();
    const tree = (locale: string) => (
      <QueryClientProvider client={client}>
        <IntlProvider locale={locale} messages={{}} onError={() => {}}>
          <ToastProvider>
            <Page />
          </ToastProvider>
        </IntlProvider>
      </QueryClientProvider>
    );
    serverBuild = "b2";
    const { rerender } = render(tree("en"));
    await waitFor(() => expect(offered).toEqual([TITLE]));
    rerender(tree("fr"));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(offered).toEqual([TITLE]);
  });
});
