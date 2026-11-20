// Settings > AI and models > Models: a model's context window, typed as 128K / 1.05M or scanned.

import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { ContextWindowScan, LLMProvider } from "@/types";
import { ProviderCard } from "./ProviderCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const platform = vi.hoisted(() => ({ aiEnabled: true }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAiEnabled: () => platform.aiEnabled,
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const PROVIDER: LLMProvider = {
  id: "p-1",
  provider: "openrouter",
  display_name: "OpenRouter",
  default_model: "model-a",
  available_models: ["model-a", "model-b"],
  context_windows: { "model-a": 1_050_000 },
  has_key: true,
  is_from_env: false,
  models_env_locked: false,
  source_integration_id: null,
};
const SCAN_URL = "/settings/llm-providers/p-1/context-windows/scan";

function scanAnswer(
  results: ContextWindowScan["results"],
  liveLookup: ContextWindowScan["live_lookup"] = "ok",
): ContextWindowScan {
  return { provider: "openrouter", live_lookup: liveLookup, results };
}

function openEditor(provider: LLMProvider = PROVIDER) {
  const onUpdateModels = vi.fn();
  renderWithProviders(
    <ProviderCard
      provider={provider}
      isAdmin
      isEditingModels
      onEditModels={() => {}}
      onCloseEditModels={() => {}}
      onDelete={() => {}}
      onRenameDisplayName={() => {}}
      onUpdateModels={onUpdateModels}
      onUpdateAccess={() => {}}
      isUpdating={false}
      platformDefaultModel={null}
      canSetPlatformDefault={false}
      onSetPlatformDefault={() => {}}
    />,
  );
  return { onUpdateModels };
}

/** The card as Settings mounts it: the editor opens and closes, the card stays. */
function Harness({
  onUpdateModels,
  provider = PROVIDER,
}: {
  onUpdateModels: (...args: unknown[]) => void;
  provider?: LLMProvider;
}) {
  const [editing, setEditing] = useState(true);
  return (
    <ProviderCard
      provider={provider}
      isAdmin
      isEditingModels={editing}
      onEditModels={() => setEditing(true)}
      onCloseEditModels={() => setEditing(false)}
      onDelete={() => {}}
      onRenameDisplayName={() => {}}
      onUpdateModels={onUpdateModels}
      onUpdateAccess={() => {}}
      isUpdating={false}
      platformDefaultModel={null}
      canSetPlatformDefault={false}
      onSetPlatformDefault={() => {}}
    />
  );
}

/** A scan answer the test releases when it chooses. */
function deferredScan() {
  let release!: (scan: ContextWindowScan) => void;
  post.mockImplementationOnce(() => new Promise((resolve) => { release = resolve as typeof release; }));
  return (scan: ContextWindowScan) => act(async () => { release(scan); });
}

const field = (model: string) =>
  screen.getByRole("textbox", { name: `Context window for ${model}` }) as HTMLInputElement;

const type = (model: string, value: string) => fireEvent.change(field(model), { target: { value } });

beforeEach(() => {
  platform.aiEnabled = true;
  get.mockReset();
  post.mockReset();
  get.mockResolvedValue({ provider: "openrouter", models: ["model-a", "model-b", "model-c"] });
});

describe("ProviderCard context windows", () => {
  it("shows a stored window in shorthand and saves a typed shorthand as tokens", async () => {
    const { onUpdateModels } = openEditor();
    await screen.findByRole("switch", { name: "model-c" });

    expect(field("model-a").value).toBe("1.05M");
    type("model-b", "128K");
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));

    expect(onUpdateModels).toHaveBeenCalledTimes(1);
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b"], {
      "model-a": 1_050_000,
      "model-b": 128_000,
    });
  });

  it("refuses to save a window that is not a token count", async () => {
    const { onUpdateModels } = openEditor();
    await screen.findByRole("switch", { name: "model-c" });

    type("model-b", "big");

    expect(field("model-b").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain("Fix the context windows marked in red");
    const save = screen.getByRole("button", { name: "Save Models" });
    expect(save.hasAttribute("disabled")).toBe(true);
    fireEvent.click(save);
    expect(onUpdateModels).not.toHaveBeenCalled();

    type("model-b", "");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(save);
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b"], { "model-a": 1_050_000 });
  });

  it("looks up the window of a model as it is enabled", async () => {
    post.mockResolvedValueOnce(scanAnswer([{ model: "model-c", context_window: 200_000, source: "provider" }]));
    const { onUpdateModels } = openEditor();

    fireEvent.click(await screen.findByRole("switch", { name: "model-c" }));

    await waitFor(() => expect(field("model-c").value).toBe("200K"));
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(SCAN_URL, { models: ["model-c"] });
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b", "model-c"], {
      "model-a": 1_050_000,
      "model-c": 200_000,
    });
  });

  it("never replaces a window that is already set, and says what it did", async () => {
    post.mockResolvedValueOnce(
      scanAnswer(
        [
          { model: "model-a", context_window: 1_000_000, source: "catalog" },
          { model: "model-b", context_window: 999, source: "provider" },
        ],
        "failed",
      ),
    );
    openEditor();
    await screen.findByRole("switch", { name: "model-c" });
    type("model-b", "64K");

    fireEvent.click(screen.getByRole("button", { name: "Scan context windows" }));

    expect(await screen.findByText("Context windows scanned")).toBeTruthy();
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(SCAN_URL, { models: ["model-a", "model-b"] });
    expect(
      screen.getByText(
        "0 context windows filled in. 2 values already set were kept. "
          + "The provider's model list could not be read: the values come from the model catalog.",
      ),
    ).toBeTruthy();
    expect(field("model-a").value).toBe("1.05M");
    expect(field("model-b").value).toBe("64K");
  });

  it("fills an empty field and counts the models no source knows", async () => {
    post.mockResolvedValueOnce(
      scanAnswer([
        { model: "model-a", context_window: 1_050_000, source: "provider" },
        { model: "model-b", context_window: null, source: null },
      ]),
    );
    openEditor();
    await screen.findByRole("switch", { name: "model-c" });

    fireEvent.click(screen.getByRole("button", { name: "Scan context windows" }));

    expect(
      await screen.findByText(
        "0 context windows filled in. 1 value already set was kept. 1 model is unknown: type its window.",
      ),
    ).toBeTruthy();
    expect(field("model-b").value).toBe("");
  });

  it("says when a scan it was asked for fails, and stays quiet on enabling a model", async () => {
    post.mockRejectedValue(new Error("Failed to fetch"));
    openEditor();

    fireEvent.click(await screen.findByRole("switch", { name: "model-c" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("Context windows not scanned")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Scan context windows" }));
    expect(await screen.findByText("Context windows not scanned")).toBeTruthy();
    expect(field("model-c").value).toBe("");
  });

  it("scans nothing with AI off, and the field stays typeable", async () => {
    platform.aiEnabled = false;
    const { onUpdateModels } = openEditor();

    fireEvent.click(await screen.findByRole("switch", { name: "model-c" }));
    const scan = screen.getByRole("button", { name: "Scan context windows" });
    expect(scan.hasAttribute("disabled")).toBe(true);
    fireEvent.click(scan);
    type("model-c", "32K");
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));

    expect(post).not.toHaveBeenCalled();
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b", "model-c"], {
      "model-a": 1_050_000,
      "model-c": 32_000,
    });
  });

  it("treats a model named like an object property as any other model", async () => {
    get.mockResolvedValue({ provider: "openrouter", models: ["model-a", "model-b", "constructor", "toString"] });
    post.mockResolvedValue(scanAnswer([{ model: "constructor", context_window: 8_192, source: "catalog" }]));
    const { onUpdateModels } = openEditor();

    fireEvent.click(await screen.findByRole("switch", { name: "toString" }));
    expect(field("toString").value).toBe("");
    fireEvent.click(screen.getByRole("switch", { name: "constructor" }));
    await waitFor(() => expect(field("constructor").value).toBe("8192"));
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));

    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b", "toString", "constructor"], {
      "model-a": 1_050_000,
      constructor: 8_192,
    });
  });

  it("waits for a lookup in flight before saving, so the window it finds is saved", async () => {
    const release = deferredScan();
    const { onUpdateModels } = openEditor();

    fireEvent.click(await screen.findByRole("switch", { name: "model-c" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const save = screen.getByRole("button", { name: "Save Models" });
    expect(save.hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("Looking up context windows");
    fireEvent.click(save);
    expect(onUpdateModels).not.toHaveBeenCalled();

    await release(scanAnswer([{ model: "model-c", context_window: 400_000, source: "provider" }]));
    expect(save.hasAttribute("disabled")).toBe(false);
    fireEvent.click(save);
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b", "model-c"], {
      "model-a": 1_050_000,
      "model-c": 400_000,
    });
  });

  it("drops the answer of a scan from an edit that was cancelled and reopened", async () => {
    const release = deferredScan();
    const onUpdateModels = vi.fn();
    renderWithProviders(<Harness onUpdateModels={onUpdateModels} />);
    await screen.findByRole("switch", { name: "model-c" });

    fireEvent.click(screen.getByRole("button", { name: "Scan context windows" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Models" }));
    await screen.findByRole("switch", { name: "model-c" });

    await release(scanAnswer([{ model: "model-b", context_window: 999, source: "provider" }]));
    expect(field("model-b").value).toBe("");
    expect(screen.queryByText("Context windows scanned")).toBeNull();
    const save = screen.getByRole("button", { name: "Save Models" });
    expect(save.hasAttribute("disabled")).toBe(false);
    fireEvent.click(save);
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b"], { "model-a": 1_050_000 });
  });
});

describe("ProviderCard gateway URL", () => {
  const renderCard = (provider: LLMProvider) =>
    renderWithProviders(
      <ProviderCard
        provider={provider}
        isAdmin
        isEditingModels={false}
        onEditModels={() => {}}
        onCloseEditModels={() => {}}
        onDelete={() => {}}
        onRenameDisplayName={() => {}}
        onUpdateModels={() => {}}
        onUpdateAccess={() => {}}
        isUpdating={false}
        platformDefaultModel={null}
        canSetPlatformDefault={false}
        onSetPlatformDefault={() => {}}
      />,
    );

  it("shows the URL of a Filigran AI Gateway provider", () => {
    renderCard({ ...PROVIDER, provider: "filigran_gateway", gateway_url: "https://gw.example.com" });
    expect(screen.getByText("https://gw.example.com")).toBeTruthy();
  });

  it("shows no URL line when the API sends none", () => {
    renderCard(PROVIDER);
    expect(screen.queryByText(/^https?:\/\//)).toBeNull();
  });
});

describe("ProviderCard retired models", () => {
  const GATEWAY: LLMProvider = { ...PROVIDER, provider: "filigran_gateway" };

  it("drops a selected model the gateway no longer offers, says so, and saves without it", async () => {
    get.mockResolvedValue({
      provider: "filigran_gateway",
      models: ["model-b", "model-c"],
      offered_models: ["model-b", "model-c", "embed-1"],
    });
    const { onUpdateModels } = openEditor(GATEWAY);

    await screen.findByRole("switch", { name: "model-c" });
    expect(screen.getByRole("status").textContent).toContain("model-a");
    expect(screen.queryByRole("switch", { name: "model-a" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));
    expect(onUpdateModels).toHaveBeenCalledWith(["model-b"], null);
  });

  it("keeps the selection of a provider whose list is not authoritative", async () => {
    get.mockResolvedValue({ provider: "openai", models: ["model-b", "model-c"] });
    const { onUpdateModels } = openEditor({ ...PROVIDER, provider: "openai" });

    await screen.findByRole("switch", { name: "model-c" });
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b"], { "model-a": 1_050_000 });
  });

  it("keeps an embedding model the gateway offers although the chat list omits it", async () => {
    get.mockResolvedValue({
      provider: "filigran_gateway",
      models: ["model-a", "model-c"],
      offered_models: ["model-a", "model-c", "embed-1"],
    });
    const { onUpdateModels } = openEditor({ ...GATEWAY, available_models: ["model-a", "embed-1"] });

    await screen.findByRole("switch", { name: "model-c" });
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));
    expect(onUpdateModels).toHaveBeenCalledWith(["embed-1", "model-a"], { "model-a": 1_050_000 });
  });

  it("prunes nothing when the backend does not say what the gateway offers", async () => {
    get.mockResolvedValue({ provider: "filigran_gateway", models: ["model-b", "model-c"] });
    const { onUpdateModels } = openEditor(GATEWAY);

    await screen.findByRole("switch", { name: "model-c" });
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b"], { "model-a": 1_050_000 });
  });

  it("says to pick a replacement when every selected model is retired, and keeps Save off", async () => {
    get.mockResolvedValue({
      provider: "filigran_gateway",
      models: ["model-c"],
      offered_models: ["model-c"],
    });
    openEditor(GATEWAY);

    await screen.findByRole("switch", { name: "model-c" });
    expect(screen.getByRole("status").textContent).toContain("Pick a replacement to enable Save");
    const save = screen.getByRole("button", { name: "Save Models" });
    expect(save.hasAttribute("disabled")).toBe(true);

    // Enabling a model also scans its context window; Save waits for the scan.
    post.mockResolvedValue(scanAnswer([]));
    fireEvent.click(screen.getByRole("switch", { name: "model-c" }));
    await waitFor(() => expect(save.hasAttribute("disabled")).toBe(false));
    expect(screen.getByRole("status").textContent).toContain("Save to apply");
  });

  it("refetches the list on every opening, and never judges the one cached by the last", async () => {
    get.mockResolvedValueOnce({
      provider: "filigran_gateway",
      models: ["model-a", "model-b"],
      offered_models: ["model-a", "model-b"],
    });
    get.mockResolvedValueOnce({
      provider: "filigran_gateway",
      models: ["model-b"],
      offered_models: ["model-b"],
    });
    const onUpdateModels = vi.fn();
    // main.tsx's 30s staleTime: unless the query is always stale, the second opening is served from cache.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: 30_000 } },
    });
    renderWithProviders(<Harness onUpdateModels={onUpdateModels} provider={GATEWAY} />, client);
    await screen.findByRole("switch", { name: "model-b" });
    expect(screen.queryByRole("status")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Models" }));

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("model-a"));
  });

  it("keeps the retired models out while a refetch is in flight", async () => {
    get.mockResolvedValueOnce({
      provider: "filigran_gateway",
      models: ["model-b", "model-c"],
      offered_models: ["model-b", "model-c"],
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: 30_000 } },
    });
    renderWithProviders(<Harness onUpdateModels={vi.fn()} provider={GATEWAY} />, client);
    await screen.findByRole("switch", { name: "model-c" });
    expect(screen.getByRole("status").textContent).toContain("model-a");

    // A window focus or a reconnect refetches the open editor's query.
    let release!: (answer: unknown) => void;
    get.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    act(() => { void client.refetchQueries({ queryKey: ["provider-models"] }); });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    expect(screen.getByRole("status").textContent).toContain("model-a");
    expect(screen.queryByRole("textbox", { name: "Context window for model-a" })).toBeNull();
    await act(async () => {
      release({ provider: "filigran_gateway", models: ["model-b", "model-c"], offered_models: ["model-b", "model-c"] });
    });
    expect(screen.getByRole("status").textContent).toContain("model-a");
  });

  it("keeps the selection when the gateway lists nothing", async () => {
    get.mockResolvedValue({ provider: "filigran_gateway", models: [], offered_models: [] });
    const { onUpdateModels } = openEditor(GATEWAY);

    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save Models" }));
    expect(onUpdateModels).toHaveBeenCalledWith(["model-a", "model-b"], { "model-a": 1_050_000 });
  });
});

describe("ProviderCard refresh of a listed catalog", () => {
  const GATEWAY: LLMProvider = {
    ...PROVIDER,
    provider: "filigran_gateway",
    display_name: null,
    is_from_env: true,
    models_env_locked: true,
    models_listed: true,
  };

  function renderCard(provider: LLMProvider, isAdmin = true) {
    renderWithProviders(
      <ProviderCard
        provider={provider}
        isAdmin={isAdmin}
        isEditingModels={false}
        onEditModels={() => {}}
        onCloseEditModels={() => {}}
        onDelete={() => {}}
        onRenameDisplayName={() => {}}
        onUpdateModels={() => {}}
        onUpdateAccess={() => {}}
        isUpdating={false}
        platformDefaultModel={null}
        canSetPlatformDefault={false}
        onSetPlatformDefault={() => {}}
      />,
    );
  }

  const refreshButton = () => screen.queryByRole("button", { name: "Refresh models" });

  it("lists the provider's models again on click", async () => {
    post.mockResolvedValue({ ...GATEWAY, available_models: ["model-a", "model-c"] });
    renderCard(GATEWAY);
    fireEvent.click(refreshButton()!);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/settings/llm-providers/p-1/refresh-models"),
    );
  });

  it("is offered only for a listed catalog, to an admin", () => {
    renderCard({ ...GATEWAY, models_listed: false });
    expect(refreshButton()).toBeNull();
  });

  it("is not offered to someone who is not an admin", () => {
    renderCard(GATEWAY, false);
    expect(refreshButton()).toBeNull();
  });

  it("is disabled while AI is off: it calls the provider", () => {
    platform.aiEnabled = false;
    renderCard(GATEWAY);
    expect(refreshButton()).toBeDisabled();
  });
});
