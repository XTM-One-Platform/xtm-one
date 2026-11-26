// The XTM Hub registration card: one listener for the popup's answer, kept
// across renders while the Hub window is open.

import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";
import { fireEvent, render, screen } from "@testing-library/react";

import { ToastProvider } from "@/components/ui/toast";
import { testQueryClient } from "@/testing/providers";
import { HOSTILE_URLS } from "@/testing/hostile-urls";
import type { XtmHubRegistration } from "@/types";
import { XtmHubRegistrationCard } from "./XtmHubRegistrationCard";

const unregistered: XtmHubRegistration = {
  status: "unregistered",
  hub_url: null,
  default_hub_url: "https://hub.example.com",
  registered_at: null,
  last_check_at: null,
  last_check_result: null,
  register_params: { platform_id: "platform-1", platform_version: "1.0.0", platform_identifier: "xtmone" },
};
let registration: XtmHubRegistration = unregistered;

const registerMutate = vi.fn();
const forgetMutate = vi.fn();
const checkMutate = vi.fn();

// Like TanStack Query v5: a new result object on every render, the same mutate.
vi.mock("@/hooks/api/useXtmHubApi", () => ({
  useXtmHubRegistration: () => ({ data: registration }),
  useRegisterXtmHub: () => ({ mutate: registerMutate, isPending: false }),
  useForgetXtmHubRegistration: () => ({ mutate: forgetMutate, isPending: false }),
  useCheckXtmHubRegistration: () => ({ mutate: checkMutate, isPending: false }),
}));

let xtmLicense: { valid: boolean; license_type: string | null } | null = null;

vi.mock("@/hooks/usePlatformConfig", () => ({
  useEdition: () => "community",
  usePlatformName: (fallback: string) => fallback,
  useXTMLicense: () => xtmLicense,
}));

// The providers with props that keep their identity across renders, so only
// the card's own dependencies can change between them.
const client = testQueryClient();
const messages = {};
const ignore = () => {};
function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <IntlProvider locale="en" messages={messages} onError={ignore}>
        <ToastProvider>{children}</ToastProvider>
      </IntlProvider>
    </QueryClientProvider>
  );
}

afterEach(() => {
  registration = unregistered;
  xtmLicense = null;
});

describe("XtmHubRegistrationCard", () => {
  it("listens once for the Hub window's answer, however often the card renders meanwhile", () => {
    const popup = { closed: false, close: vi.fn() } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);
    const listen = vi.spyOn(window, "addEventListener");
    const { rerender } = render(<XtmHubRegistrationCard />, { wrapper: Providers });

    fireEvent.click(screen.getByText("Register on the XTM Hub"));
    expect(screen.getByText("Waiting for the XTM Hub window...")).toBeTruthy();
    for (let i = 0; i < 3; i += 1) rerender(<XtmHubRegistrationCard />);

    expect(listen.mock.calls.filter(([type]) => type === "message")).toHaveLength(1);
    expect(window.open).toHaveBeenCalledTimes(1);
  });

  const registered = { ...registration, status: "registered" as const, hub_url: "https://hub.example.com" };

  it("offers to disconnect a registered platform without a trial licence", () => {
    registration = registered;
    xtmLicense = { valid: true, license_type: "standard" };
    render(<XtmHubRegistrationCard />, { wrapper: Providers });

    expect(screen.getByText("Unregister")).toBeTruthy();
    expect(screen.getByText("Forget locally")).toBeTruthy();
  });

  it("never offers to disconnect a trial's registration", () => {
    registration = registered;
    xtmLicense = { valid: true, license_type: "trial" };
    render(<XtmHubRegistrationCard />, { wrapper: Providers });

    expect(screen.getByText("Check now")).toBeTruthy();
    expect(screen.queryByText("Unregister")).toBeNull();
    expect(screen.queryByText("Forget locally")).toBeNull();
  });

  it("registers a trial only with the default XTM Hub, shown read-only", () => {
    registration = { ...registered, status: "unregistered", hub_url: null };
    xtmLicense = { valid: true, license_type: "trial" };
    render(<XtmHubRegistrationCard />, { wrapper: Providers });

    const field = screen.getByLabelText("XTM Hub URL") as HTMLInputElement;
    expect(field.value).toBe("https://hub.example.com");
    expect(field.readOnly).toBe(true);
  });

  const identifierChanged =
    "This platform's identifier changed since it was registered. Register it again to keep it on the XTM Hub.";
  const credentialsRefused =
    "The XTM Hub no longer accepts this platform's credentials. Register it again to keep it on the XTM Hub.";

  it("asks to register again when the platform's identifier changed", () => {
    registration = { ...registered, status: "needs_reregistration", last_check_result: null };
    render(<XtmHubRegistrationCard />, { wrapper: Providers });

    expect(screen.getByText(identifierChanged)).toBeTruthy();
    expect(screen.queryByText(credentialsRefused)).toBeNull();
  });

  for (const result of ["key_mismatch", "token_path_closed"]) {
    it(`asks to register again when the XTM Hub refuses the platform's credentials (${result})`, () => {
      registration = { ...registered, status: "needs_reregistration", last_check_result: result };
      render(<XtmHubRegistrationCard />, { wrapper: Providers });

      expect(screen.getByText(credentialsRefused)).toBeTruthy();
      expect(screen.queryByText(identifierChanged)).toBeNull();
    });
  }
});

// The stored Hub address, once registered: a link only when it is a web
// address (`safeExternalUrl`, i.e. `lib/safe-url.ts`), else the text.
describe("XtmHubRegistrationCard Hub link", () => {
  const renderRegistered = (hubUrl: string) => {
    registration = { ...unregistered, status: "registered", hub_url: hubUrl, registered_at: "2026-10-01T00:00:00Z" };
    return render(<XtmHubRegistrationCard />, { wrapper: Providers });
  };
  const hrefs = (root: HTMLElement) => [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

  it("links a web address", () => {
    const { container } = renderRegistered("https://hub.example.com");
    expect(hrefs(container)).toEqual(["https://hub.example.com/"]);
  });

  it.each(HOSTILE_URLS)("shows %s as text, never as a link", (_label, url) => {
    const { container } = renderRegistered(url);
    expect(hrefs(container)).toEqual([]);
    expect(container.textContent).toContain(url);
  });
});
