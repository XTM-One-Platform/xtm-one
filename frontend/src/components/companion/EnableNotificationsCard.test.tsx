// The offer to turn notifications on: one tap where the browser can, a plain
// explanation where it cannot, and nothing once the phone is linked.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const phone = vi.hoisted(() => ({
  state: "available" as string,
  linked: false,
  loading: false,
  busy: false,
  failed: false,
  link: vi.fn(async () => {}),
}));
vi.mock("./usePhoneLink", () => ({ usePhoneLink: () => phone }));

import { EnableNotificationsCard } from "./EnableNotificationsCard";

describe("EnableNotificationsCard", () => {
  it("links the phone on a tap, never on its own", () => {
    Object.assign(phone, { state: "available", linked: false, loading: false });
    renderWithProviders(<EnableNotificationsCard />);
    expect(phone.link).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Turn on notifications" }));
    expect(phone.link).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["needs-install", /install the app on your home screen first/],
    ["denied", /Allow them in your browser's site settings/],
  ])("explains what the person can do when the state is %s", (state, text) => {
    Object.assign(phone, { state, linked: false, loading: false });
    renderWithProviders(<EnableNotificationsCard />);
    expect(screen.getByText(text)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull();
  });

  it.each([
    [{ linked: true }],
    [{ state: "unsupported" }],
    [{ loading: true }],
  ])("shows nothing when %o", (patch) => {
    Object.assign(phone, { state: "available", linked: false, loading: false }, patch);
    const { container } = renderWithProviders(<EnableNotificationsCard />);
    expect(container.querySelector('[role="region"]')).toBeNull();
  });
});
