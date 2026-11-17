// A preset launched with `docker run` says it cannot start on the official images: they ship
// Node.js and uv, not the Docker CLI. Every other preset shows its own note alone.

import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { NewServerForm } from "./NewServerForm";
import { MCP_PRESETS, presetNeedsDockerCli } from "./presets";

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/hooks/useAiDisabled", () => ({ useAiActionDisabled: () => false }));
vi.mock("@/components/ui/ai-disabled", () => ({
  AiDisabledTip: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/hooks/useCredentialSharingConfirm", () => ({
  useCredentialSharingConfirm: () => [null, async () => true],
}));
vi.mock("@/hooks/api/useMCPServerApi", () => ({
  useCreateMCPServer: () => ({ mutate: vi.fn(), isPending: false }),
  useDiscoverMCPOAuth: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/ui/relay-route-field", () => ({ RelayRouteField: () => null }));
vi.mock("@/components/ui/vault-secrets", () => ({ VaultReferenceHint: () => null }));

const DOCKER_WARNING =
  "This server starts with docker run, and the official XTM One images do not include the Docker CLI: it only starts on a platform image you build with the Docker CLI, with access to a Docker daemon.";

function openPreset(name: string) {
  renderWithProviders(
    <MemoryRouter>
      <NewServerForm />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByText(name));
}

describe("presetNeedsDockerCli", () => {
  it("flags exactly the stdio presets started with docker", () => {
    expect(MCP_PRESETS.filter(presetNeedsDockerCli).map((p) => p.id).sort()).toEqual(
      ["elasticsearch", "reversinglabs", "thehive", "vulncheck"],
    );
  });

  it("leaves the Docker requirement to the shared warning, not to each note", () => {
    for (const preset of MCP_PRESETS.filter(presetNeedsDockerCli)) {
      expect(preset.notes ?? "").not.toMatch(/docker/i);
    }
  });
});

describe("NewServerForm field errors", () => {
  it("names the missing name message as the name field's description", () => {
    openPreset("GitHub");
    // Before the error the field is described by its help line; the error replaces it.
    const field = screen.getByRole("textbox", {
      name: "Server Name",
      description: "Shown in lists and to agents that use its tools.",
    });
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Create Server" }));
    expect(
      screen.getByRole("textbox", { name: "Server Name", description: "Enter a name for this server." }),
    ).toBe(field);
    expect(field.getAttribute("aria-invalid")).toBe("true");
  });
});

describe("NewServerForm preset notes", () => {
  it("warns that a docker preset needs the Docker CLI, above its own note", () => {
    openPreset("Elastic Security");
    expect(screen.getByText(DOCKER_WARNING)).toBeTruthy();
    expect(
      screen.getByText("Elastic's official server. Create an API key in Kibana under Stack Management > API keys."),
    ).toBeTruthy();
  });

  it("shows no Docker warning for a preset started with npx", () => {
    openPreset("GitHub");
    expect(screen.queryByText(DOCKER_WARNING)).toBeNull();
    expect(
      screen.getByText("Create a token at github.com/settings/tokens with repo, read:org, read:user scopes."),
    ).toBeTruthy();
  });
});
