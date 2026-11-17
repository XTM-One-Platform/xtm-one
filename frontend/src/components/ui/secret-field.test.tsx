// SecretField: a credential is typed masked, or picked from a vault the user is granted.

import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { PickerVaultProvider } from "@/types";
import { SecretField } from "./secret-field";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const CORP: PickerVaultProvider = {
  name: "corp",
  display_name: "Corporate Vault",
  type: "hashicorp",
  source: "database",
  browsable: true,
  paths: ["secret/soc"],
};

function answer(path: string): unknown {
  if (path === "/secret-vaults") return [CORP];
  if (path.startsWith("/secret-vaults/corp/secrets")) {
    return {
      entries: [{ name: "github", path: "secret/soc/github", folder: false }],
      next_page_token: null,
      unavailable: null,
    };
  }
  if (path.startsWith("/secret-vaults/corp/keys")) return { keys: ["token", "user"], single_value: false, unavailable: null };
  throw new Error(`unexpected GET ${path}`);
}

function Harness({ initial = "", onValue }: { initial?: string; onValue?: (value: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <SecretField
      label="API key"
      value={value}
      onChange={(next) => {
        setValue(next);
        onValue?.(next);
      }}
    />
  );
}

describe("SecretField", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation(async (path: string) => answer(path) as never);
  });

  it("shows a typed value masked and offers the vaults the user may use", async () => {
    renderWithProviders(<Harness initial="sk-typed" />);
    expect((screen.getByLabelText("API key") as HTMLInputElement).type).toBe("password");
    expect(await screen.findByRole("button", { name: /From a vault/ })).toBeInTheDocument();
  });

  it("offers no vault when the user is granted none", async () => {
    get.mockImplementation(async (path: string) => (path === "/secret-vaults" ? [] : answer(path)) as never);
    renderWithProviders(<Harness />);
    await waitFor(() => expect(get).toHaveBeenCalledWith("/secret-vaults"));
    expect(screen.queryByRole("button", { name: /From a vault/ })).not.toBeInTheDocument();
  });

  it("picks a secret by browsing, checks it on the server and stores the reference, never a value", async () => {
    post.mockResolvedValue({ ok: true, reason: null, description: null });
    const picked = vi.fn();
    renderWithProviders(<Harness onValue={picked} />);

    fireEvent.click(await screen.findByRole("button", { name: /From a vault/ }));
    fireEvent.click(await screen.findByRole("option", { name: /github/ }));
    await waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringContaining("/secret-vaults/corp/keys?path=secret%2Fsoc%2Fgithub")));

    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("The secret can be read.")).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith("/secret-vaults/check", { reference: "vault://corp/secret/soc/github" });

    fireEvent.click(screen.getByRole("button", { name: "Use this secret" }));
    expect(picked).toHaveBeenLastCalledWith("vault://corp/secret/soc/github");
    const chip = await screen.findByTestId("vault-reference-chip");
    expect(chip).toHaveTextContent("Corporate Vault");
    expect(chip).toHaveTextContent("secret/soc/github");
  });

  it("refuses a path outside the user's grant before anything is sent", async () => {
    renderWithProviders(<Harness />);
    fireEvent.click(await screen.findByRole("button", { name: /From a vault/ }));
    fireEvent.change(await screen.findByLabelText("Path"), { target: { value: "secret/admin/root" } });
    expect(await screen.findByText("This path is outside the paths you may use on this vault.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use this secret" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("shows a picked reference as a chip that can be cleared", async () => {
    renderWithProviders(<Harness initial="vault://corp/secret/soc/github#token" />);
    const chip = await screen.findByTestId("vault-reference-chip");
    expect(chip).toHaveTextContent("#token");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByTestId("vault-reference-chip")).not.toBeInTheDocument();
  });

  it("never turns a reference into a chip while it is typed, only once the user leaves the field", async () => {
    renderWithProviders(<Harness />);
    const input = screen.getByLabelText("API key");
    fireEvent.change(input, { target: { value: "vault://corp/secret/soc/github" } });
    expect(screen.queryByTestId("vault-reference-chip")).not.toBeInTheDocument();
    expect(screen.getByLabelText("API key")).toBe(input);
    fireEvent.blur(input);
    expect(await screen.findByTestId("vault-reference-chip")).toHaveTextContent("secret/soc/github");
  });

  it("clears a stored value, says it will be removed, and keeps it on Keep", async () => {
    function Stored() {
      const [cleared, setCleared] = useState(false);
      return (
        <SecretField
          label="Password"
          value=""
          onChange={() => undefined}
          hasStoredValue
          cleared={cleared}
          onClear={() => setCleared(true)}
          onUndoClear={() => setCleared(false)}
        />
      );
    }
    renderWithProviders(<Stored />);
    expect(screen.getByLabelText("Password")).toHaveAttribute("placeholder", "Stored: type to replace");
    fireEvent.click(screen.getByRole("button", { name: "Clear the stored value" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("placeholder", "Will be removed");
    expect(screen.getByLabelText("Password")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Keep/ }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("placeholder", "Stored: type to replace");
  });

  it("lets a user with no granted vault replace a stored reference by typing", async () => {
    get.mockImplementation(async (path: string) => (path === "/secret-vaults" ? [] : answer(path)) as never);
    renderWithProviders(
      <SecretField
        label="Password"
        value=""
        onChange={() => undefined}
        hasStoredValue
        stored={{ provider: "corp", provider_display_name: "Corporate Vault", path: null, key: null, entry: null }}
      />,
    );
    expect(await screen.findByText("a path you may not see")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("placeholder", "Stored: type to replace");
  });

  it("shows a credential that is not secret by itself as typed, with no reveal toggle", () => {
    renderWithProviders(<SecretField label="Username" value="demo@example.test" onChange={() => undefined} concealed={false} />);
    expect((screen.getByLabelText("Username") as HTMLInputElement).type).toBe("text");
    expect(screen.queryByRole("button", { name: /^Show/ })).not.toBeInTheDocument();
  });

  it("names each reveal toggle after its field, so two secrets have two distinct toggles", () => {
    renderWithProviders(
      <>
        <SecretField label="Public key" value="" onChange={() => undefined} allowVault={false} />
        <SecretField label="Secret key" value="" onChange={() => undefined} allowVault={false} />
      </>,
    );
    expect(screen.getByRole("button", { name: "Show Public key" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show Secret key" }));
    expect(screen.getByRole("button", { name: "Hide Secret key" })).toBeInTheDocument();
    expect((screen.getByLabelText("Secret key") as HTMLInputElement).type).toBe("text");
  });

  it("shows a 1Password field by its label in the key picker and stores its id", async () => {
    const op: PickerVaultProvider = { ...CORP, name: "op", display_name: "1Password", type: "onepassword", paths: null };
    get.mockImplementation(async (path: string) => {
      if (path === "/secret-vaults") return [op] as never;
      if (path.startsWith("/secret-vaults/op/secrets")) {
        return { entries: [{ name: "Entra demo", path: "vlt/itm", folder: false }], next_page_token: null, unavailable: null } as never;
      }
      if (path.startsWith("/secret-vaults/op/keys")) {
        return { keys: ["TOTP_k3j", "password"], labels: { TOTP_k3j: "one-time password" }, single_value: false, unavailable: null } as never;
      }
      throw new Error(`unexpected GET ${path}`);
    });
    renderWithProviders(<Harness />);
    fireEvent.click(await screen.findByRole("button", { name: /From a vault/ }));
    const item = await screen.findByRole("option", { name: /Entra demo/ });
    expect(item).toHaveTextContent("itm");
    fireEvent.click(item);
    await waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringContaining("/secret-vaults/op/keys")));
    await userEvent.setup().click(await screen.findByRole("combobox", { name: "Key" }));
    expect(await screen.findByRole("option", { name: /one-time password/ })).toHaveTextContent("TOTP_k3j");
  });
});
