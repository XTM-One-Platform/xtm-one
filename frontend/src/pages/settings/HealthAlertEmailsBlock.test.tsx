// Settings > Platform Health, the alert emails block (#101 S12b): off by
// default, recipients checked with the server's rule, Save off while invalid.
// Asserted on roles, text and test ids (src/no-class-name-assertions.test.ts).

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { AlertEmailsBlock, alertEmailsValid } from "@/pages/settings/HealthAlertEmailsBlock";
import { renderWithProviders } from "@/testing/providers";
import type { HealthAlertEmailSettings, HealthSettings } from "@/types";

const off: HealthAlertEmailSettings = { enabled: false, recipients: [], min_severity: "warning", immediate: true, digest: true };
const withAlerts = (alert_emails: HealthAlertEmailSettings) => ({ alert_emails }) as unknown as HealthSettings;

afterEach(cleanup);

describe("AlertEmailsBlock", () => {
  it("reads Off at rest and hides the rest of the settings", () => {
    renderWithProviders(<AlertEmailsBlock draft={withAlerts(off)} editing={false} onChange={() => {}} />);
    const block = screen.getByTestId("settings.health.alert-emails");
    expect(block).toHaveTextContent("Send alert emails");
    expect(block).toHaveTextContent("Off");
    expect(screen.queryByTestId("health.read.alert-recipients")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("reads the recipients, severity and switches at rest when on", () => {
    const on = { ...off, enabled: true, recipients: ["soc@example.com", "ops@example.com"], min_severity: "critical" as const, digest: false };
    renderWithProviders(<AlertEmailsBlock draft={withAlerts(on)} editing={false} onChange={() => {}} />);
    expect(screen.getByTestId("health.read.alert-recipients")).toHaveTextContent("soc@example.com, ops@example.com");
    expect(screen.getByTestId("settings.health.alert-emails")).toHaveTextContent("Critical only");
  });

  it("turns the alert emails on in the draft", () => {
    const onChange = vi.fn();
    renderWithProviders(<AlertEmailsBlock draft={withAlerts(off)} editing onChange={onChange} />);
    fireEvent.click(screen.getByRole("switch", { name: "Send alert emails" }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ alert_emails: expect.objectContaining({ enabled: true }) }));
  });

  it("marks the recipients field invalid when on with no recipient", () => {
    renderWithProviders(<AlertEmailsBlock draft={withAlerts({ ...off, enabled: true })} editing onChange={() => {}} />);
    const field = screen.getByRole("textbox", { name: "Recipients" });
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("Add at least one recipient, or turn the alert emails off.");
  });

  it("describes the switch and the selected severity under each field", () => {
    renderWithProviders(<AlertEmailsBlock draft={withAlerts({ ...off, enabled: true, recipients: ["soc@example.com"] })} editing onChange={() => {}} />);
    expect(screen.getByRole("switch", { name: "Send alert emails" })).toHaveAccessibleDescription("Emails the recipients below about health findings.");
    expect(screen.getByTestId("settings.health.alert-emails")).toHaveTextContent("The digest lists warning and critical findings.");
  });

  it("names the address the server would refuse", () => {
    renderWithProviders(<AlertEmailsBlock draft={withAlerts({ ...off, enabled: true, recipients: ["soc@example.com", "nobody"] })} editing onChange={() => {}} />);
    expect(screen.getByRole("textbox", { name: "Recipients" })).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("Not an email address: nobody");
  });
});

describe("alertEmailsValid", () => {
  it("follows the server's recipient rule", () => {
    const valid = (address: string) => alertEmailsValid(withAlerts({ ...off, enabled: true, recipients: [address] }));
    expect(valid("soc@example.com")).toBe(true);
    expect(valid("nobody")).toBe(false);
    expect(valid("a@b")).toBe(false);
    expect(valid("a@@b.com")).toBe(false);
    expect(valid("a b@c.com")).toBe(false);
  });

  it("accepts off and refuses on without recipients, bad ones or too many", () => {
    expect(alertEmailsValid(withAlerts(off))).toBe(true);
    expect(alertEmailsValid(withAlerts({ ...off, enabled: true }))).toBe(false);
    expect(alertEmailsValid(withAlerts({ ...off, enabled: true, recipients: ["soc@example.com"] }))).toBe(true);
    expect(alertEmailsValid(withAlerts({ ...off, recipients: ["bad"] }))).toBe(false);
    const many = Array.from({ length: 21 }, (_, i) => `a${i}@example.com`);
    expect(alertEmailsValid(withAlerts({ ...off, enabled: true, recipients: many }))).toBe(false);
  });

  it("bounds the alert delay from 0 to a day in whole minutes", () => {
    const valid = (alert_after_minutes: number) => alertEmailsValid(withAlerts({ ...off, alert_after_minutes }));
    expect(valid(15)).toBe(true);
    expect(valid(0)).toBe(true);
    expect(valid(1440)).toBe(true);
    expect(valid(-1)).toBe(false);
    expect(valid(1441)).toBe(false);
    expect(valid(Number.NaN)).toBe(false);
  });
});

describe("AlertEmailsBlock alert delay", () => {
  it("edits the delay of the immediate email, 15 minutes by default", () => {
    const onChange = vi.fn();
    renderWithProviders(<AlertEmailsBlock draft={withAlerts({ ...off, enabled: true, recipients: ["soc@example.com"] })} editing onChange={onChange} />);
    const field = screen.getByRole("spinbutton", { name: "Send after" });
    expect(field).toHaveValue(15);
    fireEvent.change(field, { target: { value: "30" } });
    const calls = onChange.mock.calls;
    expect(calls[calls.length - 1]?.[0].alert_emails.alert_after_minutes).toBe(30);
  });

  it("reads the delay at rest when immediate emails are on", () => {
    renderWithProviders(
      <AlertEmailsBlock draft={withAlerts({ ...off, enabled: true, recipients: ["soc@example.com"], alert_after_minutes: 20 })} editing={false} onChange={() => {}} />,
    );
    expect(screen.getByTestId("settings.health.alert-emails")).toHaveTextContent("20 minutes");
  });
});
