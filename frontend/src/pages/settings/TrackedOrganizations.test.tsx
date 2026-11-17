// Settings > Organization, tracked organizations (#9157, slice M2; form dialog and OpenCTI picker, #9375 slice 3): add, edit and archive a tracked provider.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Organization } from "@/hooks/api/useOrganizationsApi";
import { TrackedOrganizations } from "./TrackedOrganizations";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const patch = vi.mocked(api.patch);

const PRIMARY: Organization = {
  id: "p1",
  name: "Acme",
  role: "primary",
  relationship: null,
  criticality: "high",
  opencti_organization_id: null,
  monitoring: "off",
  created_at: "2026-11-06T10:00:00Z",
  archived_at: null,
};
const PROVIDER: Organization = {
  ...PRIMARY,
  id: "t1",
  name: "Cloudy SaaS",
  role: "tracked",
  relationship: "provider",
  criticality: "critical",
  opencti_organization_id: "identity--1",
};

let organizations: Organization[] = [];

beforeEach(() => {
  vi.clearAllMocks();
  organizations = [PRIMARY];
  get.mockImplementation(async (path: string) => {
    if (path === "/organizations") return organizations;
    throw new Error(`unexpected GET ${path}`);
  });
});

describe("TrackedOrganizations", () => {
  it("shows the empty state when only the primary exists, never the primary as a row", async () => {
    renderWithProviders(<TrackedOrganizations />);
    expect(await screen.findByText("No tracked organization yet")).toBeTruthy();
    expect(screen.queryByText("Acme")).toBeNull();
  });

  it("adds a tracked provider with its relationship and criticality", async () => {
    post.mockResolvedValue({ ...PROVIDER, id: "t2", name: "Mail Co" });
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByText("No tracked organization yet");
    fireEvent.click(screen.getByRole("button", { name: /Add an organization/ }));
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "  Mail Co " } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]).toEqual([
      "/organizations",
      { name: "Mail Co", relationship: "provider", criticality: "medium", opencti_organization_id: null },
    ]);
  });

  it("refuses an empty name on the field itself, without calling the API", async () => {
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByText("No tracked organization yet");
    fireEvent.click(screen.getByRole("button", { name: /Add an organization/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    const name = screen.getByLabelText(/^Name/);
    expect(name.getAttribute("aria-invalid")).toBe("true");
    const message = await screen.findByText("A name is required");
    // The error alone describes the field: no library counter read with it (B2 of #9443).
    expect(name.getAttribute("aria-describedby")?.split(" ")).toEqual([message.id]);
    expect(screen.queryByText("0/200")).toBeNull();
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(name, { target: { value: "M" } });
    expect(name.getAttribute("aria-invalid")).toBeNull();
    expect(screen.queryByText("A name is required")).toBeNull();
  });

  it("never carries a refused empty name into the next form opened after Cancel", async () => {
    organizations = [PRIMARY, PROVIDER];
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByTestId("settings.organizations.row");
    fireEvent.click(screen.getByRole("button", { name: /Add an organization/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(screen.getByLabelText(/^Name/).getAttribute("aria-invalid")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("settings.organizations.form")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Edit Cloudy SaaS" }));
    const name = screen.getByLabelText(/^Name/);
    expect(name.getAttribute("aria-invalid")).toBeNull();
    expect(screen.queryByText("A name is required")).toBeNull();
  });

  it("names a medium criticality as a level, never in the time sense of the bare Medium key (fr)", async () => {
    organizations = [PRIMARY, { ...PROVIDER, criticality: "medium" }];
    renderWithProviders(<TrackedOrganizations />, { locale: "fr" });
    const row = await screen.findByTestId("settings.organizations.row");
    expect(row.textContent).toContain("Criticit\u00e9\u00a0: Moyenne");
    expect(row.textContent).not.toContain("Moyen terme");
  });

  it("lists a tracked provider and edits it in place", async () => {
    organizations = [PRIMARY, PROVIDER];
    patch.mockResolvedValue({ ...PROVIDER, name: "Cloudy" });
    renderWithProviders(<TrackedOrganizations />);
    const row = await screen.findByTestId("settings.organizations.row");
    expect(row.textContent).toContain("Cloudy SaaS");
    expect(row.textContent).toContain("Provider");
    expect(row.textContent).toContain("Linked to OpenCTI");
    expect(row.textContent).toContain("Monitoring off");
    fireEvent.click(screen.getByRole("button", { name: "Edit Cloudy SaaS" }));
    expect(screen.getByRole("heading", { name: "Edit Cloudy SaaS" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "Cloudy" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0]).toEqual([
      "/organizations/t1",
      { name: "Cloudy", relationship: "provider", criticality: "critical", opencti_organization_id: "identity--1" },
    ]);
  });

  it("archives a tracked provider after the confirmation", async () => {
    organizations = [PRIMARY, PROVIDER];
    post.mockResolvedValue({ ...PROVIDER, archived_at: "2026-11-06T12:00:00Z" });
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByTestId("settings.organizations.row");
    fireEvent.click(screen.getByRole("button", { name: "Archive Cloudy SaaS" }));
    fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organizations/t1/archive", {}));
  });

  it("carries a long organization name whole in the title of its truncated row name (A1 of #9227)", async () => {
    const long = "Very Long Managed Security Services Provider International Holdings Group";
    organizations = [PRIMARY, { ...PROVIDER, name: long }];
    renderWithProviders(<TrackedOrganizations />);
    const row = await screen.findByTestId("settings.organizations.row");
    const name = Array.from(row.querySelectorAll("span")).find((el) => el.textContent === long);
    expect(name?.getAttribute("title")).toBe(long);
  });

  it("moves focus to the next row's Edit button once an archived row leaves the list (A3 of #9227)", async () => {
    const second: Organization = { ...PROVIDER, id: "t2", name: "Mail Co" };
    organizations = [PRIMARY, PROVIDER, second];
    post.mockImplementation(async () => {
      organizations = [PRIMARY, second];
      return { ...PROVIDER, archived_at: "2026-11-06T12:00:00Z" };
    });
    renderWithProviders(<TrackedOrganizations />);
    await screen.findAllByTestId("settings.organizations.row");
    fireEvent.click(screen.getByRole("button", { name: "Archive Cloudy SaaS" }));
    fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Edit Cloudy SaaS" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Edit Mail Co" })));
  });

  it("moves focus to Add an organization once the last tracked row is archived (A3 of #9227)", async () => {
    organizations = [PRIMARY, PROVIDER];
    post.mockImplementation(async () => {
      organizations = [PRIMARY];
      return { ...PROVIDER, archived_at: "2026-11-06T12:00:00Z" };
    });
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByTestId("settings.organizations.row");
    fireEvent.click(screen.getByRole("button", { name: "Archive Cloudy SaaS" }));
    fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
    await screen.findByText("No tracked organization yet");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: /Add an organization/ })));
  });

  it("translates the OpenCTI organization field in fr and es", () => {
    expect(fr["OpenCTI organization"]).toBe("Organisation OpenCTI");
    expect(es["OpenCTI organization"]).toBe("Organizaci\u00f3n de OpenCTI");
  });

  it("opens add in a centred dialog with full-size fields, a help line under each and no raw OpenCTI id input (#9375 slice 3)", async () => {
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByText("No tracked organization yet");
    fireEvent.click(screen.getByRole("button", { name: /Add an organization/ }));
    const form = await screen.findByTestId("settings.organizations.form");
    expect(form.tagName).toBe("FORM");
    // A centred dialog, never a drawer (Sam, 7 Nov 2026).
    expect(screen.getByRole("dialog").contains(form)).toBe(true);
    expect(screen.getByRole("heading", { name: "Add an organization" })).toBeTruthy();
    const name = screen.getByLabelText(/^Name/);
    const described = (name.getAttribute("aria-describedby") ?? "").split(" ").map((id) => document.getElementById(id)?.textContent);
    expect(described).toContain("The name agents use for it in briefs and reports.");
    expect(screen.getByText("How your organization depends on it or serves it.")).toBeTruthy();
    expect(screen.getByText("How hard a breach at this organization would hit yours.")).toBeTruthy();
    expect(screen.getByText("Search the connected OpenCTI by name or alias. Optional.")).toBeTruthy();
    expect(screen.getByText("Off. Nothing is watched in OpenCTI and no run is counted until an administrator enables it.")).toBeTruthy();
    expect(screen.queryByLabelText("OpenCTI organization id (optional)")).toBeNull();
  });

  it("shows Unsaved changes only once a field differs from what the dialog opened with", async () => {
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByText("No tracked organization yet");
    fireEvent.click(screen.getByRole("button", { name: /Add an organization/ }));
    expect(screen.queryByText("Unsaved changes")).toBeNull();
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "Mail Co" } });
    expect(screen.getByText("Unsaved changes")).toBeTruthy();
  });

  it("searches OpenCTI organizations as you type and links the one picked, never asking before a character", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organizations") return organizations;
      if (path.startsWith("/organizations/opencti-identities?search=")) {
        return [{ id: "identity--9", name: "Mail Co Group", aliases: ["MailCo"], organization_type: "vendor" }];
      }
      throw new Error(`unexpected GET ${path}`);
    });
    post.mockResolvedValue({ ...PROVIDER, id: "t2", name: "Mail Co" });
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByText("No tracked organization yet");
    fireEvent.click(screen.getByRole("button", { name: /Add an organization/ }));
    expect(get.mock.calls.some(([path]) => String(path).includes("opencti-identities"))).toBe(false);
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "Mail Co" } });
    const picker = screen.getByRole("combobox", { name: "OpenCTI organization" });
    fireEvent.focus(picker);
    fireEvent.change(picker, { target: { value: "mail" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/organizations/opencti-identities?search=mail"), { timeout: 2000 });
    fireEvent.click(await screen.findByText("Mail Co Group"));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]).toEqual([
      "/organizations",
      { name: "Mail Co", relationship: "provider", criticality: "medium", opencti_organization_id: "identity--9" },
    ]);
  });

  it("keeps the dialog and what was typed when OpenCTI does not answer, with the error on the picker (B1 of #9443)", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organizations") return organizations;
      if (path.startsWith("/organizations/opencti-identities?search=")) throw new Error("Could not connect to OpenCTI");
      throw new Error(`unexpected GET ${path}`);
    });
    // The app's own policy: every query throws to the error boundary unless it opts out (main.tsx).
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true }, mutations: { retry: false } } });
    renderWithProviders(<TrackedOrganizations />, client);
    await screen.findByText("No tracked organization yet");
    fireEvent.click(screen.getByRole("button", { name: /Add an organization/ }));
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "Mail Co" } });
    const picker = screen.getByRole("combobox", { name: "OpenCTI organization" });
    fireEvent.focus(picker);
    fireEvent.change(picker, { target: { value: "acme" } });
    const message = await screen.findByText("OpenCTI did not answer. Check the OpenCTI integration, then search again.", {}, { timeout: 2000 });
    // The results list says the search failed, never that nothing matched; the raw backend detail is not shown (UX round 2 B1).
    expect(await screen.findByText("OpenCTI did not answer")).toBeTruthy();
    expect(screen.queryByText("No OpenCTI organization matches")).toBeNull();
    expect(screen.queryByText(/Could not connect to OpenCTI/)).toBeNull();
    expect(screen.getByTestId("settings.organizations.form")).toBeTruthy();
    expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe("Mail Co");
    const field = screen.getByRole("combobox", { name: "OpenCTI organization" });
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(field.getAttribute("aria-describedby")?.split(" ")).toContain(message.id);
  });

  it("names the linked OpenCTI organization when Edit opens, never its raw id (B5 of #9443)", async () => {
    organizations = [PRIMARY, PROVIDER];
    get.mockImplementation(async (path: string) => {
      if (path === "/organizations") return organizations;
      if (path === "/organizations/opencti-identities/identity--1") return { id: "identity--1", name: "Cloudy SaaS Inc", aliases: [], organization_type: "vendor" };
      throw new Error(`unexpected GET ${path}`);
    });
    renderWithProviders(<TrackedOrganizations />);
    await screen.findByTestId("settings.organizations.row");
    fireEvent.click(screen.getByRole("button", { name: "Edit Cloudy SaaS" }));
    const picker = screen.getByRole("combobox", { name: "OpenCTI organization" }) as HTMLInputElement;
    await waitFor(() => expect(picker.value).toBe("Cloudy SaaS Inc"));
    expect(document.body.textContent).not.toContain("identity--1");
  });

  it("lists the organizations in a table with a search, filters and a count (B4 of #9443)", async () => {
    const second: Organization = { ...PROVIDER, id: "t2", name: "Mail Co", relationship: "customer", criticality: "low", opencti_organization_id: null };
    organizations = [PRIMARY, PROVIDER, second];
    renderWithProviders(<TrackedOrganizations />);
    await screen.findAllByTestId("settings.organizations.row");
    expect(screen.getByTestId("settings.organizations.count").textContent).toBe("2 organizations");
    expect(screen.getByRole("columnheader", { name: /Criticality/ })).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search organizations..." }), { target: { value: "mail" } });
    await waitFor(() => expect(screen.getAllByTestId("settings.organizations.row")).toHaveLength(1));
    expect(within(screen.getByTestId("settings.organizations.row")).getByText("Mail Co")).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search organizations..." }), { target: { value: "" } });
    // The table applies a typed search after a pause: wait for the cleared search to bring both rows back.
    await waitFor(() => expect(screen.getAllByTestId("settings.organizations.row")).toHaveLength(2));

    // The filters are the table's own pills (gate 3 of dev-docs/reference/data-tables.md).
    // The pill, not the column header's sort button of the same name: only the pill opens a panel.
    fireEvent.click(screen.getByRole("button", { name: "OpenCTI", expanded: false }));
    fireEvent.click(within(await screen.findByRole("group", { name: "OpenCTI" })).getByRole("button", { name: "Linked to OpenCTI" }));
    await waitFor(() => expect(screen.getAllByTestId("settings.organizations.row")).toHaveLength(1));
    expect(within(screen.getByTestId("settings.organizations.row")).getByText("Cloudy SaaS")).toBeTruthy();
  });

  it("hides an archived organization", async () => {
    organizations = [PRIMARY, { ...PROVIDER, archived_at: "2026-11-06T12:00:00Z" }];
    renderWithProviders(<TrackedOrganizations />);
    expect(await screen.findByText("No tracked organization yet")).toBeTruthy();
  });
});
