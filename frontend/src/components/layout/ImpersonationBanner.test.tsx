import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

/**
 * The impersonation banner renders only while an admin acts as another user,
 * which is why no browser pass ever saw it, and why its unreadable label in the
 * light theme shipped. `fixed-fill-text-contrast.test.ts` holds the
 * contrast of the tokens; this holds that the banner is still dressed in them,
 * whatever its markup becomes.
 */

const auth = {
  user: null as Record<string, unknown> | null,
  stopImpersonation: vi.fn(),
};

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => auth }));

const { ImpersonationBanner } = await import("./AppLayout");

beforeEach(() => {
  auth.user = null;
  auth.stopImpersonation.mockReset();
});

function bannerOf(text: string | RegExp): HTMLElement {
  const banner = screen.getByText(text).closest(".fixed");
  expect(banner).not.toBeNull();
  return banner as HTMLElement;
}

describe("ImpersonationBanner", () => {
  it("renders nothing for a user who is not impersonating", () => {
    auth.user = { full_name: "Ada Admin", email: "ada@example.com", impersonating: false };
    const { container } = render(<ImpersonationBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names who is impersonated, on the amber fill with the fixed-fill text", () => {
    auth.user = {
      full_name: "Bob Analyst",
      email: "bob@example.com",
      impersonating: true,
      impersonated_by_name: "Ada Admin",
    };
    render(<ImpersonationBanner />);
    const banner = bannerOf("Bob Analyst");
    expect(banner).toHaveTextContent("Impersonating Bob Analyst (bob@example.com) — logged in as Ada Admin");
    const classes = banner.className.split(/\s+/);
    expect(classes).toContain("bg-feedback-alert-primary/90");
    expect(classes).toContain("text-on-feedback-fill");
    // Nothing inside may switch back to the label that inverts with the theme.
    expect(banner.outerHTML).not.toContain("text-negative-primary");
  });

  it("stops impersonating from its button, which has a hover step of its own", () => {
    auth.user = { full_name: "Bob Analyst", email: "bob@example.com", impersonating: true };
    render(<ImpersonationBanner />);
    const stop = screen.getByRole("button", { name: /Stop Impersonating/ });
    const classes = stop.className.split(/\s+/);
    const rest = classes.find((c) => c.startsWith("bg-"));
    const hover = classes.find((c) => c.startsWith("hover:bg-"));
    expect([rest, hover]).toEqual(["bg-on-feedback-fill/10", "hover:bg-on-feedback-fill/20"]);
    fireEvent.click(stop);
    expect(auth.stopImpersonation).toHaveBeenCalledTimes(1);
  });
});
