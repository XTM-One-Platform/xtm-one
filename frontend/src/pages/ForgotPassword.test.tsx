/**
 * ForgotPassword field error: the error under the email field is the
 * field's accessible description while it shows, so a screen reader reads the
 * reason with "invalid" (WCAG 3.3.1). `yarn test:unit` (vitest + happy-dom).
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

import ForgotPassword from "./ForgotPassword";

vi.mock("@/hooks/api/useAuthApi", () => ({
  useForgotPassword: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useAuthConfig: () => ({ data: { local_enabled: true } }),
}));
vi.mock("@/hooks/usePlatformConfig", () => ({
  usePlatformName: () => "XTM One",
  usePlatformLogoUrl: () => "",
}));

describe("ForgotPassword email error", () => {
  it("is named by the email field's aria-describedby only while it shows", () => {
    renderWithProviders(
      <MemoryRouter>
        <ForgotPassword />
      </MemoryRouter>,
    );
    const field = screen.getByRole("textbox", { name: "Email" });
    expect(field.getAttribute("aria-describedby")).toBeNull();

    fireEvent.change(field, { target: { value: "not-an-email" } });
    fireEvent.submit(field.closest("form") as HTMLFormElement);

    expect(
      screen.getByRole("textbox", { name: "Email", description: "Please enter a valid email address" }),
    ).toBe(field);
    expect(field.getAttribute("aria-invalid")).toBe("true");

    fireEvent.change(field, { target: { value: "user@mail.com" } });
    expect(field.getAttribute("aria-describedby")).toBeNull();
  });
});
