// The Supabase Database access tab: the probe it runs on open, the choices it
// offers from the answer, and the extra_config a save sends.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Integration, SupabaseProbeResponse } from "@/types";
import { SupabaseDatabaseAccessTab } from "./SupabaseDatabaseAccessTab";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const REF = "abcdefghijklmnopqrst";
const POOLER = "aws-0-eu-west-1.pooler.supabase.com";

const integration = (extra: Record<string, unknown>) =>
  ({ id: "i-1", provider: "supabase", auth_type: "api_key", name: "Supabase", extra_config: extra }) as unknown as Integration;

const PROBED: SupabaseProbeResponse = {
  projects: [{ ref: REF, name: "Production", region: "eu-west-1", status: "ACTIVE_HEALTHY" }],
  roles: [
    { name: "agent_reader", bypass_rls: false },
    { name: "etl", bypass_rls: true },
  ],
  connections: [{ host: POOLER, port: 5432, mode: "session" }],
  databases: ["postgres"],
  role_bypasses_rls: false,
  functions: [
    {
      schema: "public",
      name: "get_document",
      signature: "p_document_id uuid",
      returns: "text",
      returns_set: false,
      security_definer: true,
      volatility: "s",
      comment: "One document by id",
    },
    {
      schema: "public",
      name: "get_document_excerpt",
      signature: "p_document_id uuid",
      returns: "SETOF record",
      returns_set: true,
      security_definer: true,
      volatility: "s",
      comment: null,
    },
  ],
  readable_relations: [{ schema: "public", name: "documents", kind: "r", rls_enabled: true }],
  errors: {},
};

const STORED = {
  project_ref: REF,
  db_role: "agent_reader",
  db_password: "********",
  db_host: POOLER,
  db_port: "5432",
  db_name: "postgres",
  db_functions: "public.get_document",
  other: "kept",
};

describe("SupabaseDatabaseAccessTab", () => {
  beforeEach(() => {
    vi.mocked(api.post).mockReset();
    vi.mocked(api.put).mockReset();
  });

  it("probes the stored settings on open and shows what the role can reach", async () => {
    vi.mocked(api.post).mockResolvedValue(PROBED);
    renderWithProviders(<SupabaseDatabaseAccessTab integration={integration(STORED)} />);

    await screen.findByText("public.get_document(p_document_id uuid) → text");
    expect(api.post).toHaveBeenCalledWith("/integrations/i-1/supabase/probe", {
      project_ref: REF,
      db_host: POOLER,
      db_port: "5432",
      db_name: "postgres",
      db_role: "agent_reader",
      db_ca_cert: "",
    });
    expect(screen.getByText("Row level security applies to this role.")).toBeTruthy();
    expect(screen.getByText("Tables the role can still read directly")).toBeTruthy();
    expect(screen.getByText("public.documents")).toBeTruthy();
    expect(screen.getByRole("switch", { name: "Allow public.get_document" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("switch", { name: "Allow public.get_document_excerpt" }).getAttribute("aria-checked")).toBe("false");
  });

  it("saves the allowed functions with the stored password kept", async () => {
    vi.mocked(api.post).mockResolvedValue(PROBED);
    vi.mocked(api.put).mockResolvedValue({});
    renderWithProviders(<SupabaseDatabaseAccessTab integration={integration(STORED)} />);

    fireEvent.click(await screen.findByRole("switch", { name: "Allow public.get_document_excerpt" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.put).toHaveBeenCalledOnce());
    expect(api.put).toHaveBeenCalledWith("/integrations/i-1", {
      extra_config: {
        ...STORED,
        db_functions: "public.get_document,public.get_document_excerpt",
      },
    });
  });

  it("removes the role by sending every stored db key as null", async () => {
    vi.mocked(api.post).mockResolvedValue(PROBED);
    vi.mocked(api.put).mockResolvedValue({});
    renderWithProviders(<SupabaseDatabaseAccessTab integration={integration(STORED)} />);

    fireEvent.click(await screen.findByRole("button", { name: "Remove the role" }));
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => expect(api.put).toHaveBeenCalledOnce());
    expect(api.put).toHaveBeenCalledWith("/integrations/i-1", {
      extra_config: {
        project_ref: REF,
        other: "kept",
        db_role: null,
        db_password: null,
        db_host: null,
        db_port: null,
        db_name: null,
        db_functions: null,
      },
    });
  });

  it("names the part the probe could not reach and offers typed fields instead", async () => {
    vi.mocked(api.post).mockResolvedValue({
      ...PROBED,
      projects: null,
      roles: null,
      connections: null,
      databases: null,
      role_bypasses_rls: null,
      functions: null,
      readable_relations: null,
      errors: { management: "No access token is configured: type the project, host and role instead." },
    } satisfies SupabaseProbeResponse);
    renderWithProviders(<SupabaseDatabaseAccessTab integration={integration({})} />);

    expect(
      await screen.findByText("Supabase account: No access token is configured: type the project, host and role instead."),
    ).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Role" })).toBeTruthy();
    // Nothing to save without a role, a host and a password.
    expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(true);
    expect(screen.queryByRole("button", { name: "Remove the role" })).toBeNull();
  });
});
