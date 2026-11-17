// The Profile's API keys read "Last used" in the viewer's language:
// the row went through the English `timeAgo()` of lib/utils.ts, so a French
// page showed "Dernière utilisation 5m ago".

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { UserApiKey } from "@/types";
import { PersonalApiKeys } from "./PersonalApiKeys";

const minutesAgo = (n: number) => new Date(Date.now() - n * 60 * 1000).toISOString();

const KEY: UserApiKey = {
  id: "k1", name: "CI key", key_prefix: "xtm_abcd", is_active: true, is_from_env: false,
  last_used_at: minutesAgo(5), request_count: 12, created_at: minutesAgo(600),
  surfaces: [], all_surfaces: true, read_only: false, agent_ids: null, agents: [],
  expires_at: null, expired: false, revoked_at: null, rate_limit_per_minute: null,
  quota_limit: null, quota_period: null, quota_used: null, full_scope: true, beyond_max_lifetime: false,
};

vi.mock("@/hooks/api/useMiscApi", () => ({
  useApiKeys: () => ({ data: [KEY] }),
  useApiKeyPolicy: () => ({ data: { max_lifetime_days: 0 } }),
  useDeleteApiKey: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isImpersonating: false }) }));
vi.mock("./ApiKeyDialog", () => ({ ApiKeyDialog: () => null }));

describe("PersonalApiKeys last used", () => {
  it("reads the last use in French", () => {
    renderWithProviders(<PersonalApiKeys platformName="XTM One" />, { locale: "fr" });
    expect(screen.getByText("Dernière utilisation il y a 5 minutes")).toBeTruthy();
    expect(screen.queryByText(/ago/)).toBeNull();
  });

  it("reads the last use in Japanese", () => {
    renderWithProviders(<PersonalApiKeys platformName="XTM One" />, { locale: "ja" });
    expect(screen.getByText("最終使用：5 分前")).toBeTruthy();
    expect(screen.queryByText(/ago/)).toBeNull();
  });
});
