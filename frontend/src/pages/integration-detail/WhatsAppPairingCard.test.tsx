// The WhatsApp gateway Pairing card: the live pairing state, the QR while it is
// `qr`, and the gateway webhook URL for a user who may manage the integration.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Integration, ProviderCatalogItem, WhatsAppPairing } from "@/types";
import { WebhooksCard } from "./WebhooksCard";
import { WhatsAppPairingCard } from "./WhatsAppPairingCard";
import { isWhatsAppGateway } from "./whatsapp-gateway";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const integration = (extra: Record<string, unknown> = { backend: "linked_device_gateway" }, provider = "whatsapp") =>
  ({ id: "i-1", provider, name: "WhatsApp", extra_config: extra }) as unknown as Integration;

const answer = (overrides: Partial<WhatsAppPairing> = {}): WhatsAppPairing => ({
  vendor: "whapi",
  state: "connected",
  error: null,
  can_manage: true,
  qr: null,
  webhook_path: "/api/channels/whatsapp/gateway/i-1?token=abc",
  has_webhook_secret: true,
  ...overrides,
});

describe("isWhatsAppGateway", () => {
  it("is true only for a whatsapp integration on the linked-device gateway", () => {
    expect(isWhatsAppGateway(integration())).toBe(true);
    expect(isWhatsAppGateway(integration({ backend: "meta_cloud_api" }))).toBe(false);
    expect(isWhatsAppGateway(integration({}))).toBe(false);
    expect(isWhatsAppGateway(integration({ backend: "linked_device_gateway" }, "slack"))).toBe(false);
  });
});

describe("WhatsAppPairingCard", () => {
  it("shows the QR and the webhook URL to a manager while waiting for a scan", async () => {
    vi.mocked(api.get).mockResolvedValue(answer({ state: "qr", qr: "data:image/png;base64,AAA" }));
    renderWithProviders(<WhatsAppPairingCard integration={integration()} baseUrl="https://one.example" />);

    expect(await screen.findByText("Waiting for a scan")).toBeTruthy();
    expect(screen.getByAltText("WhatsApp pairing QR code").getAttribute("src")).toBe("data:image/png;base64,AAA");
    expect(screen.getByText("https://one.example/api/channels/whatsapp/gateway/i-1?token=abc")).toBeTruthy();
    expect(api.get).toHaveBeenCalledWith("/integrations/i-1/whatsapp/pairing");
  });

  it("shows a paired number without a QR", async () => {
    vi.mocked(api.get).mockResolvedValue(answer());
    renderWithProviders(<WhatsAppPairingCard integration={integration()} baseUrl="https://one.example" />);

    expect(await screen.findByText("Paired")).toBeTruthy();
    expect(screen.queryByAltText("WhatsApp pairing QR code")).toBeNull();
  });

  it("tells a viewer that the QR and the webhook URL are for managers, without a scan instruction", async () => {
    vi.mocked(api.get).mockResolvedValue(answer({ state: "qr", can_manage: false, webhook_path: null }));
    renderWithProviders(<WhatsAppPairingCard integration={integration()} baseUrl="https://one.example" />);

    expect(
      await screen.findByText("The number is waiting to be paired. A user who can manage this integration scans the QR code here."),
    ).toBeTruthy();
    expect(screen.queryByText(/scan this code/)).toBeNull();
    expect(
      screen.getByText("Only a user who can manage this integration sees the webhook URL: it carries the webhook secret."),
    ).toBeTruthy();
  });

  it("asks for a webhook secret when none is set", async () => {
    vi.mocked(api.get).mockResolvedValue(answer({ webhook_path: null, has_webhook_secret: false }));
    renderWithProviders(<WhatsAppPairingCard integration={integration()} baseUrl="https://one.example" />);

    expect(
      await screen.findByText("Set a webhook secret on this integration first: without one, the gateway's deliveries are refused."),
    ).toBeTruthy();
  });

  it.each([
    ["gateway_unreachable", "The gateway could not be reached. Check its base URL and that the service is up."],
    ["no_channel_token", "No channel token yet. Paste the gateway's channel token into API key."],
    ["token_unreadable", "The channel token could not be read. Save it again in API key."],
    ["unknown_vendor", "This gateway vendor is not supported."],
  ] as const)("turns the error code %s into a sentence, the raw detail only in the tooltip", async (code, sentence) => {
    const raw = "WhatsApp gateway: read channel health failed (401)";
    vi.mocked(api.get).mockResolvedValue(answer({ state: "unknown", error_code: code, error: raw }));
    renderWithProviders(<WhatsAppPairingCard integration={integration()} baseUrl="https://one.example" />);

    const shown = await screen.findByText(sentence);
    expect(shown.getAttribute("title")).toBe(raw);
    expect(screen.queryByText(raw)).toBeNull();
  });
});

describe("WebhooksCard on a WhatsApp integration", () => {
  const catalog = { name: "WhatsApp", webhook_path: "/api/channels/whatsapp/webhook", available_events: [] } as unknown as ProviderCatalogItem;

  it("hides the Meta webhook endpoint on the gateway backend, whose URL is on the pairing card", () => {
    renderWithProviders(<WebhooksCard integration={integration()} catalogEntry={catalog} baseUrl="https://one.example" />);
    expect(screen.queryByText("https://one.example/api/channels/whatsapp/webhook")).toBeNull();
  });

  it("keeps the Meta webhook endpoint on the Meta backend", () => {
    renderWithProviders(
      <WebhooksCard integration={integration({ backend: "meta_cloud_api" })} catalogEntry={catalog} baseUrl="https://one.example" />,
    );
    expect(screen.getByText("https://one.example/api/channels/whatsapp/webhook")).toBeTruthy();
  });

  it("titles the callback block like the events block and points at the provider's webhook settings", () => {
    renderWithProviders(
      <WebhooksCard integration={integration({ backend: "meta_cloud_api" })} catalogEntry={catalog} baseUrl="https://one.example" />,
    );
    expect(screen.getByText("WhatsApp Webhook URL")).toBeTruthy();
    expect(screen.getByText("Set this URL as the callback URL in the WhatsApp webhook settings of your developer app.")).toBeTruthy();
    expect(screen.queryByText("Webhook Endpoint")).toBeNull();
  });
});

describe("one copyable URL row on the integration page", () => {
  it("pairs a <code> with CopyIconButton only in CopyableUrl", () => {
    const dir = __dirname;
    const offenders = readdirSync(dir)
      .filter((name) => name.endsWith(".tsx") && !name.endsWith(".test.tsx") && name !== "CopyableUrl.tsx")
      .filter((name) => {
        const source = readFileSync(join(dir, name), "utf8");
        return source.includes("<CopyIconButton") && source.includes("<code");
      });
    expect(offenders).toEqual([]);
  });
});
