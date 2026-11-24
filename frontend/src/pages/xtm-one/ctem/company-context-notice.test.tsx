// The organization context notice of every CTEM page (#11464): missing (set
// it up), partial (the empty fields named) or a quiet confirmation; never the
// former Company Context knowledge base. Two placements (#11586): on the home
// dashboard for administrators only and dismissible per person, back when a
// new field goes missing; on a brief for every reader and never dismissible.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { renderWithProviders } from "@/testing/providers";
import { CompanyContextNotice, ContextScoredLine } from "./CompanyContextNotice";
import { GenericContextNotice } from "./GenericContextNotice";
import { contextNoticeVisible, withDismissed } from "./notice-dismissal";

const auth = vi.hoisted(() => ({
  isAdmin: true,
  dismissed: null as string[] | null,
  writes: [] as unknown[],
}));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ isAdmin: auth.isAdmin, user: { organization_context_notice_dismissed: auth.dismissed } }),
}));
vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  // The server stores what is sent: the next read of /me answers it.
  useUpdateProfile: () => ({
    mutate: (body: { organization_context_notice_dismissed?: string[] | null }) => {
      auth.writes.push(body);
      auth.dismissed = body.organization_context_notice_dismissed ?? null;
    },
  }),
}));

type Context = Parameters<typeof CompanyContextNotice>[0]["context"];

function notice(context: Context, placement: "dashboard" | "brief" = "dashboard") {
  return renderWithProviders(
    <MemoryRouter>
      <CompanyContextNotice context={context} placement={placement} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  auth.isAdmin = true;
  auth.dismissed = null;
  auth.writes = [];
});

describe("CompanyContextNotice on the home dashboard", () => {
  it("asks an administrator to set the context up when none is saved", () => {
    notice({ state: "missing", missing_fields: ["crown_jewels"] });
    expect(screen.getByText("Set up your organization context")).toBeTruthy();
    expect(screen.getByTestId("ctem-context-missing")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Set up organization context" }).getAttribute("href")).toContain("/settings/organization");
  });

  it("names the empty fields when the context is partial, with the link", () => {
    notice({ state: "partial", missing_fields: ["software", "crown_jewels"] });
    expect(screen.getByText("Your organization context is incomplete")).toBeTruthy();
    expect(screen.getByTestId("ctem-context-partial").textContent).toContain("Software, Primary assets and business services");
    expect(screen.getByRole("link", { name: "Complete organization context" })).toBeTruthy();
  });

  it("is never shown to a user who cannot edit the organization context", () => {
    auth.isAdmin = false;
    const { container } = notice({ state: "partial", missing_fields: ["software"] });
    expect(container.textContent).toBe("");
    notice({ state: "missing", missing_fields: ["crown_jewels"] });
    expect(screen.queryByTestId("ctem-context-missing")).toBeNull();
  });

  it("is dismissed per user, stays hidden on reload, comes back when a new field goes missing", () => {
    const partial = { state: "partial" as const, missing_fields: ["software", "crown_jewels"] };
    const first = notice(partial);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss until a new field is missing" }));
    expect(auth.writes).toEqual([{ organization_context_notice_dismissed: ["software", "crown_jewels"] }]);
    first.unmount();

    // Reload: the stored list is read back from the user.
    const reload = notice(partial);
    expect(reload.container.textContent).toBe("");
    reload.unmount();

    // A field filled since: still hidden.
    const fewer = notice({ state: "partial", missing_fields: ["software"] });
    expect(fewer.container.textContent).toBe("");
    fewer.unmount();

    // A newly missing field: shown again.
    notice({ state: "partial", missing_fields: ["software", "crown_jewels", "currency"] });
    expect(screen.getByTestId("ctem-context-partial")).toBeTruthy();
  });

  it("leaves a complete context to the toolbar line, no row of its own (#12258)", () => {
    const { container } = notice({ state: "complete", missing_fields: [] });
    expect(container.textContent).toBe("");
    expect(screen.queryByTestId("ctem-context-complete")).toBeNull();
  });

  it("draws the toolbar confirmation as an inline line", () => {
    renderWithProviders(<ContextScoredLine />);
    const line = screen.getByTestId("ctem-context-complete");
    expect(line.tagName).toBe("SPAN");
    expect(line.textContent).toContain("Scored against your organization context.");
  });

  it("draws nothing before the posture is read", () => {
    const { container } = notice(undefined);
    expect(container.textContent).toBe("");
  });
});

describe("CompanyContextNotice on a brief", () => {
  it("shows every reader the missing fields, with no close control and no link", () => {
    auth.isAdmin = false;
    auth.dismissed = ["software"];
    notice({ state: "partial", missing_fields: ["software"] }, "brief");
    expect(screen.getByText("Organization context incomplete")).toBeTruthy();
    expect(screen.getByTestId("ctem-context-partial").textContent).toContain("Missing: Software. The analysis would be sharper with them.");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("gives an administrator the link, still not dismissible", () => {
    notice({ state: "missing", missing_fields: ["crown_jewels"] }, "brief");
    expect(screen.getByText("No organization context")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Set up organization context" })).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("leaves a complete context to the page header, no row of its own", () => {
    const { container } = notice({ state: "complete", missing_fields: [] }, "brief");
    expect(container.textContent).toBe("");
    expect(screen.queryByTestId("ctem-context-complete")).toBeNull();
    expect(screen.queryByTestId("ctem-context-partial")).toBeNull();
    expect(screen.queryByTestId("ctem-context-missing")).toBeNull();
  });

  it("draws the header confirmation as an inline line, the sentence in its title", () => {
    renderWithProviders(<ContextScoredLine inline />);
    const line = screen.getByTestId("ctem-context-complete");
    expect(line.tagName).toBe("SPAN");
    expect(line.textContent).toContain("Scored against your organization context.");
    expect(line.getAttribute("title")).toBe("Scored against your organization context.");
    // Its type size and width are left to the design-system gates: tests never
    // assert on class names (src/no-class-name-assertions.test.ts).
  });
});

describe("GenericContextNotice on a home widget", () => {
  const generic = () =>
    renderWithProviders(
      <MemoryRouter>
        <GenericContextNotice title="These actions are generic" adminText="admin" readerText="reader" testId="ctem-act-now-generic" />
      </MemoryRouter>,
    );

  it("is dismissible for every reader and stays closed on reload", () => {
    auth.isAdmin = false;
    const first = generic();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(auth.writes).toEqual([{ organization_context_notice_dismissed: ["generic:ctem-act-now-generic"] }]);
    first.unmount();
    expect(generic().container.textContent).toBe("");
  });
});

describe("notice dismissal rule", () => {
  it("shows until closed, then only for a field outside what was closed", () => {
    expect(contextNoticeVisible(["a"], null)).toBe(true);
    expect(contextNoticeVisible(["a", "b"], ["a", "b"])).toBe(false);
    expect(contextNoticeVisible(["a"], ["a", "b"])).toBe(false);
    expect(contextNoticeVisible(["a", "c"], ["a", "b"])).toBe(true);
  });

  it("keeps what was closed before and adds the new keys once", () => {
    expect(withDismissed(["generic:x"], ["a", "generic:x"])).toEqual(["generic:x", "a"]);
    expect(withDismissed(null, ["a"])).toEqual(["a"]);
  });
});

describe("placements", () => {
  it("the home dashboard dismisses, every brief keeps it; no page reads the Company Context knowledge base", () => {
    const dir = join(process.cwd(), "src", "pages", "xtm-one");
    const placements: Record<string, string> = {
      "CtemCommandCenter.tsx": "dashboard",
      "CtemStageDetail.tsx": "brief",
      "ExposureBriefing.tsx": "brief",
    };
    for (const [file, placement] of Object.entries(placements)) {
      const text = readFileSync(join(dir, file), "utf8");
      expect(text, file).not.toMatch(/useKnowledgeBases|Company Context knowledge base|COMPANY_CONTEXT_KB/);
      expect(text, file).toMatch(new RegExp(`<CompanyContextNotice\\b[^>]*placement="${placement}"`));
    }
  });

  it("the home dashboard draws the complete line inside its toolbar row, after the context bar (#12258)", () => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "CtemCommandCenter.tsx"), "utf8");
    const toolbar = text.indexOf('data-testid="ctem-toolbar"');
    const bar = text.indexOf("<DashboardContextBar", toolbar);
    const line = text.indexOf("<ContextScoredLine", bar);
    const rightGroup = text.indexOf("ml-auto", bar);
    expect(toolbar).toBeGreaterThan(-1);
    expect(bar).toBeGreaterThan(toolbar);
    expect(line).toBeGreaterThan(bar);
    expect(line).toBeLessThan(rightGroup);
  });

  it("a stage page draws the complete line on the header's breadcrumb line, before the notice row", () => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "CtemStageDetail.tsx"), "utf8");
    const extras = text.indexOf("crumbExtras={");
    const line = text.indexOf("<ContextScoredLine inline", extras);
    const title = text.indexOf("title={data.label}", extras);
    const notice = text.indexOf("<CompanyContextNotice", extras);
    expect(extras).toBeGreaterThan(-1);
    expect(line).toBeGreaterThan(extras);
    expect(line).toBeLessThan(title);
    expect(notice).toBeGreaterThan(title);
  });

  it("the exposure briefing draws the complete line on the header's breadcrumb line, before the notice row", () => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "ExposureBriefing.tsx"), "utf8");
    const extras = text.indexOf("crumbExtras={");
    const line = text.indexOf("<ContextScoredLine inline", extras);
    const title = text.indexOf("title={t_i18n(\"Exposure briefing\")}", extras);
    const notice = text.indexOf("<CompanyContextNotice", extras);
    expect(extras).toBeGreaterThan(-1);
    expect(line).toBeGreaterThan(extras);
    expect(line).toBeLessThan(title);
    expect(notice).toBeGreaterThan(title);
  });
});
