// The power button of an agent card is icon-only: its accessible name states
// the action it takes on that agent, in every locale.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";

import fr from "@/i18n/lang/fr.json";
import es from "@/i18n/lang/es.json";
import ja from "@/i18n/lang/ja.json";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import type { Agent } from "@/types";
import { AgentActions } from "./Agents";

const mutate = vi.fn();
vi.mock("@/hooks/api/useAgentApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAgentApi")>()),
  useUpdateAgent: () => ({ mutate }),
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

function agent(enabled: boolean): Agent {
  return { id: "a1", name: "Analyst", enabled, disable_chat: false, user_id: "me" } as unknown as Agent;
}

function actions(enabled: boolean) {
  return (
    <MemoryRouter>
      <AgentActions agent={agent(enabled)} isAdmin userId="me" chatEnabled={false} isLinkedGA={false} />
    </MemoryRouter>
  );
}

describe("AgentActions power button", () => {
  it("is named after the action it takes on an enabled agent and disables it", () => {
    renderWithProviders(actions(true));
    fireEvent.click(screen.getByRole("button", { name: "Disable agent" }));
    expect(mutate).toHaveBeenCalledWith({ id: "a1", enabled: false });
  });

  it("is named Enable agent on a disabled agent", () => {
    renderWithProviders(actions(false));
    expect(screen.getByRole("button", { name: "Enable agent" })).toBeTruthy();
  });

  it.each([
    ["fr", fr, "Désactiver l'agent", "Activer l'agent"],
    ["es", es, "Desactivar el agente", "Activar el agente"],
    ["ja", ja, "エージェントを無効化", "エージェントを有効化"],
  ] as const)("carries the %s name for both states", (locale, messages, disable, enable) => {
    for (const [enabled, name] of [[true, disable], [false, enable]] as const) {
      const view = render(
        <QueryClientProvider client={testQueryClient()}>
          <IntlProvider locale={locale} messages={messages} onError={() => {}}>
            {actions(enabled)}
          </IntlProvider>
        </QueryClientProvider>,
      );
      expect(screen.getByRole("button", { name })).toBeTruthy();
      view.unmount();
    }
  });
});
