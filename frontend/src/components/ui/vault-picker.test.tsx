// VaultSecretPicker: a large vault is listed page after page, and a typed path is read once.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { PickerVaultProvider } from "@/types";
import { VaultSecretPicker } from "./vault-picker";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

const CORP: PickerVaultProvider = {
  name: "corp",
  display_name: "Corporate Vault",
  type: "hashicorp",
  source: "database",
  browsable: true,
  paths: null,
};

/** 150 secrets, answered 100 per page like the server does. */
const NAMES = Array.from({ length: 150 }, (_, index) => `s${String(index).padStart(3, "0")}`);

function answer(path: string): unknown {
  if (path === "/secret-vaults") return [CORP];
  if (path.startsWith("/secret-vaults/corp/secrets")) {
    const second = path.includes("page_token=p2");
    const names = second ? NAMES.slice(100) : NAMES.slice(0, 100);
    return {
      entries: names.map((name) => ({ name, path: `app/${name}`, folder: false })),
      next_page_token: second ? null : "p2",
      unavailable: null,
    };
  }
  if (path.startsWith("/secret-vaults/corp/keys")) return { keys: ["token"], single_value: false, unavailable: null };
  throw new Error(`unexpected GET ${path}`);
}

function calls(fragment: string): string[] {
  return get.mock.calls.map(([path]) => String(path)).filter((path) => path.includes(fragment));
}

function renderPicker() {
  return renderWithProviders(<VaultSecretPicker open onOpenChange={() => undefined} onPick={() => undefined} />);
}

describe("VaultSecretPicker", () => {
  beforeEach(() => {
    get.mockReset();
    get.mockImplementation(async (path: string) => answer(path) as never);
  });

  it("lists a vault of 150 secrets whole with Load more, and says the list is partial until then", async () => {
    renderPicker();
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(100));
    expect(screen.getByTestId("vault-picker-partial")).toHaveTextContent("100 names loaded");

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(150));
    expect(calls("/secrets")).toEqual(["/secret-vaults/corp/secrets?prefix=", "/secret-vaults/corp/secrets?prefix=&page_token=p2"]);
    expect(screen.queryByTestId("vault-picker-partial")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("searches past the first page: a name on the next page is found", async () => {
    renderPicker();
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(100));

    fireEvent.change(screen.getByLabelText("Search secrets..."), { target: { value: "s142" } });
    expect(await screen.findByRole("option", { name: /s142/ })).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(calls("/secrets")).toHaveLength(2);
  });

  it("reads the keys of a typed path once, when the user leaves the field, never per keystroke", async () => {
    renderPicker();
    const input = await screen.findByLabelText("Path");
    const typed = "secret/team/service";
    for (let end = 1; end <= typed.length; end += 1) {
      fireEvent.change(input, { target: { value: typed.slice(0, end) } });
    }
    // Longer than any debounce: still nothing read while the field has the focus.
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(calls("/keys")).toEqual([]);

    fireEvent.blur(input);
    await waitFor(() => expect(calls("/keys")).toEqual(["/secret-vaults/corp/keys?path=secret%2Fteam%2Fservice"]));
    fireEvent.blur(input);
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls("/keys")).toHaveLength(1);
  });
});
