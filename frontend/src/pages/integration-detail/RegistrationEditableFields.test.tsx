// The fields a platform registration leaves to the deployment: the OpenAEV tenant
// and the webhook secret OpenAEV / OpenCTI send, which is never shown.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Integration } from "@/types";
import { RegistrationEditableFields } from "./RegistrationEditableFields";

const mutate = vi.fn();
vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useUpdateIntegration: () => ({ mutate, isPending: false }),
}));

const MASK = "********";
const LABELS = { tenant_id: "Tenant ID", webhook_secret: "Webhook secret" };

const integration = (extra: Record<string, string>) =>
  ({ id: "i-1", provider: "openaev", name: "OpenAEV", extra_config: { base_url: "https://aev.test", ...extra } }) as unknown as Integration;

const renderFields = (extra: Record<string, string>) =>
  renderWithProviders(
    <RegistrationEditableFields integration={integration(extra)} fieldKeys={["tenant_id", "webhook_secret"]} labels={LABELS} canEdit />,
  );

describe("RegistrationEditableFields", () => {
  it("says a stored webhook secret is stored and never shows it", () => {
    renderFields({ tenant_id: "t-b", webhook_secret: MASK });

    expect(screen.getByText("t-b")).toBeTruthy();
    expect(screen.getByText("Stored")).toBeTruthy();
    expect(screen.queryByText(MASK)).toBeNull();
  });

  it("edits the webhook secret in a masked input and keeps it when left empty", () => {
    mutate.mockClear();
    renderFields({ tenant_id: "t-b", webhook_secret: MASK });
    fireEvent.click(screen.getByRole("button", { name: /Edit/ }));

    const secret = screen.getByLabelText("Webhook secret");
    expect(secret).toHaveAttribute("type", "password");
    expect(secret).toHaveAttribute("placeholder", "Stored: type to replace");
    expect(secret).toHaveValue("");

    fireEvent.change(screen.getByLabelText("Tenant ID"), { target: { value: "t-c" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(mutate).toHaveBeenCalledOnce();
    expect(mutate.mock.calls[0][0]).toEqual({ id: "i-1", extra_config: { tenant_id: "t-c", webhook_secret: MASK } });
  });

  it("sets a webhook secret the registration left empty", () => {
    mutate.mockClear();
    renderFields({});
    expect(screen.getAllByText("—")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: /Edit/ }));

    const secret = screen.getByLabelText("Webhook secret");
    expect(secret).toHaveAttribute("placeholder", "Not set");
    fireEvent.change(secret, { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(mutate.mock.calls[0][0]).toEqual({ id: "i-1", extra_config: { tenant_id: "", webhook_secret: "s3cret" } });
  });

  it("shows a choice field (OpenCTI Live writes) by its label, unset as its default", () => {
    const options = {
      live_writes: [
        { value: "off", label: "Off: changes are staged in a draft for review (default)" },
        { value: "on", label: "On: changes go to the live graph, no review" },
      ],
    };
    const render = (extra: Record<string, string>) =>
      renderWithProviders(
        <RegistrationEditableFields
          integration={integration(extra)}
          fieldKeys={["webhook_secret", "live_writes"]}
          labels={{ webhook_secret: "Webhook secret", live_writes: "Live writes" }}
          options={options}
          canEdit
        />,
      );
    const { unmount } = render({});
    expect(screen.getByText("Off: changes are staged in a draft for review (default)")).toBeTruthy();
    unmount();
    render({ live_writes: "on" });
    expect(screen.getByText("On: changes go to the live graph, no review")).toBeTruthy();
    expect(screen.getByText("Default for bound agents; an agent can override it on its Tooling tab. Kept when the platform registers it again.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Edit/ }));
    expect(screen.getByLabelText("Live writes")).toBeTruthy();
  });

  it("keeps a help line under every field and puts Cancel before Save changes", () => {
    renderFields({ tenant_id: "t-b" });
    expect(screen.getAllByText("Kept when the platform registers this integration again.")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: /Edit/ }));
    expect(screen.getByLabelText("Tenant ID")).toHaveAccessibleDescription("Kept when the platform registers this integration again.");
    const names = screen.getAllByRole("button").map((button) => button.textContent);
    expect(names.indexOf("Cancel")).toBeLessThan(names.indexOf("Save changes"));
  });
});
