import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import type { NavSection } from "./nav-model";

/**
 * The navigation adapter, not the library.
 *
 * `Navbar`, `NavbarItem` and `NavbarSubmenu` are the design system's and carry
 * their own tests; what can break here is the mapping — a section that should
 * be a submenu rendered as flat rows, a row that stops being a real link, or
 * `aria-current` going missing, which is the ONLY thing the library reads to
 * mark a row selected — and the shell OpenCTI and OpenAEV share: the product
 * switcher on top, no section heading, "Made by Filigran" at the foot. None of
 * those fail a typecheck, and the visual capture covers the shell's geometry
 * rather than its wiring.
 */

const sections: NavSection[] = [
  { id: "main", entries: [{ kind: "leaf", to: "/", icon: "house", label: "Home" }] },
  {
    id: "resources",
    // A label on a plain block, to prove the shell does not draw it.
    label: "Resources",
    entries: [
      {
        kind: "group",
        id: "studio",
        label: "Agent Studio",
        icon: "sparkles",
        to: "/agent-studio",
        children: [
          { kind: "leaf", to: "/knowledge", icon: "book-text", label: "Knowledge" },
          { kind: "leaf", to: "/prompts", icon: "file-text", label: "Prompts" },
          // A locked row of a group: dimmed and marked, still a live link.
          { kind: "leaf", to: "/intents", icon: "zap", label: "Intents", locked: true, lockReason: "ai_disabled" },
        ],
      },
    ],
  },
  {
    id: "observability",
    label: "Observability",
    collapsible: true,
    icon: "activity",
    entries: [{ kind: "leaf", to: "/observability/traces", icon: "list-tree", label: "Traces" }],
  },
];

const railState = { rail: false };
const branding: { emblem: string | null } = { emblem: "/emblem.svg" };
const viewer = { isAdmin: true };
const mode = { xtmOne: true };
let registrations: unknown[] = [];
let hubRegistration: { status: string; hub_url: string | null; default_hub_url: string } = {
  status: "unregistered",
  hub_url: null,
  default_hub_url: "https://hub.default.example.test",
};

vi.mock("./nav-model", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./nav-model")>();
  return { ...actual, resolveNav: () => sections };
});

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: viewer.isAdmin }) }));
vi.mock("@/hooks/usePlatformConfig", () => ({
  usePlatformConfig: () => ({ features: { ai_adoption: true } }),
  useIsXtmOne: () => mode.xtmOne,
  usePlatformName: () => "XTM One",
  usePlatformLogoUrl: () => "/logo.png",
  usePlatformEmblemUrl: () => branding.emblem,
  useChatEnabled: () => true,
  useCustomVariablesEnabled: () => true,
  // Added by main's AI-disabled platform mode: the Sidebar reads it to
  // dim and lock the AI areas.
  useAiEnabled: () => true,
  // The licensing rework's second lock reason: an entry can be marked because
  // the administrator switched AI off, or because the deployment has no
  // licence, and the two carry different explanations.
  useAgenticAIEnabled: () => true,
}));
vi.mock("@/hooks/api/usePlatformApi", () => ({
  usePlatformRegistrations: () => ({ data: registrations }),
}));
vi.mock("@/hooks/api/useXtmHubApi", () => ({
  useXtmHubRegistrationStatus: () => ({ data: hubRegistration }),
}));
// Main's relays read the API through their own hook; the Sidebar only
// turns the answer into an entry.
vi.mock("@/hooks/api/useRelayApi", () => ({ useRelaysEnabled: () => false }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => false }));
// The AI-off explanation the locked rows carry, set per test.
const lockState: { aiReason: string | null } = { aiReason: null };
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useAiDisabled")>();
  return { ...actual, useAiDisabledReason: () => lockState.aiReason };
});

const { Sidebar } = await import("./Sidebar");
// The real store, not a mock: what breaks is how the accordion and the rail's
// flyouts drive it, which a mock answering a constant cannot show.
const { NavPrefsProvider } = await import("@/hooks/useNavPrefs");

const EXPANDED_KEY = "copilot-nav:expanded";
const SEEDED_EXPANDED = { studio: true, observability: true };

/** Where the router is, for the assertions on what a click navigated to. */
function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderSidebar(route = "/") {
  localStorage.setItem("copilot-nav:rail", railState.rail ? "1" : "0");
  return renderWithProviders(
    <MemoryRouter initialEntries={[route]}>
      <NavPrefsProvider>
        <Sidebar />
      </NavPrefsProvider>
      <LocationProbe />
    </MemoryRouter>,
    { locale: "en-us" },
  );
}

/** The groups the full-width sidebar shows expanded, as persisted. */
function expandedGroups(): Record<string, boolean> {
  return JSON.parse(localStorage.getItem(EXPANDED_KEY) ?? "{}");
}

beforeEach(() => {
  registrations = [];
  hubRegistration = { status: "unregistered", hub_url: null, default_hub_url: "https://hub.default.example.test" };
  railState.rail = false;
  viewer.isAdmin = true;
  mode.xtmOne = true;
  branding.emblem = "/emblem.svg";
  lockState.aiReason = null;
  localStorage.clear();
  localStorage.setItem(EXPANDED_KEY, JSON.stringify(SEEDED_EXPANDED));
});

/** The image the home link draws: the logo is decorative, the link is named. */
function homeImage() {
  return screen.getByRole("link", { name: "XTM One home" }).querySelector("img")?.getAttribute("src");
}

/** The switcher's rows, opened the way a pointer opens a Radix menu. */
function switcherRows() {
  const trigger = screen.getByRole("button", { name: "Switch product" });
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: "mouse" });
  fireEvent.click(trigger);
  return screen.getAllByRole("menuitem").map((item) => item.closest("a") ?? item.querySelector("a") ?? item);
}

describe("Sidebar", () => {
  it("renders the navigation landmark the library owns", () => {
    renderSidebar();
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
  });

  it("renders leaves as real links, not click handlers", () => {
    renderSidebar();
    const home = screen.getByRole("link", { name: /Home/ });
    expect(home).toHaveAttribute("href", "/");
  });

  it("marks the active route with aria-current, which is how the library shows selection", () => {
    renderSidebar("/observability/traces");
    const traces = screen.getByRole("link", { name: /Traces/ });
    expect(traces).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: /Home/ })).not.toHaveAttribute("aria-current");
  });

  it("turns a collapsible section into one submenu rather than flat rows", () => {
    renderSidebar();
    // The trigger row carries the section's label; its leaf is nested inside.
    expect(screen.getByText("Observability")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Traces/ })).toBeInTheDocument();
  });

  it("opens a group's hub page from its heading and leaves the group open", () => {
    // The hand-rolled sidebar's label was a link to the hub; the library's
    // expanded heading is an accordion trigger, so the click is read off its
    // toggle. The group here is open: the click that would fold it navigates
    // instead.
    renderSidebar("/");
    fireEvent.click(screen.getByRole("button", { name: /Agent Studio/ }));
    expect(screen.getByTestId("location").textContent).toBe("/agent-studio");
    expect(expandedGroups().studio).toBe(true);
    // ...and its pages are there, with no extra row standing in for the hub.
    expect(screen.getByRole("link", { name: /Knowledge/ })).toHaveAttribute("href", "/knowledge");
    expect(screen.queryByRole("link", { name: /Overview/ })).not.toBeInTheDocument();
  });

  it("folds the group from its heading once on the hub, which it marks as the current page", () => {
    renderSidebar("/agent-studio");
    const heading = screen.getByRole("button", { name: /Agent Studio/ });
    expect(heading).toHaveAttribute("aria-current", "page");
    fireEvent.click(heading);
    expect(expandedGroups().studio).toBe(false);
    expect(screen.queryByRole("link", { name: /Knowledge/ })).not.toBeInTheDocument();
    expect(screen.getByTestId("location").textContent).toBe("/agent-studio");
  });

  it("separates blocks with a rule and no heading, as OpenCTI and OpenAEV do", () => {
    renderSidebar();
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).queryByText("Resources")).not.toBeInTheDocument();
    expect(within(nav).getAllByRole("separator").length).toBeGreaterThanOrEqual(2);
  });

  it("lists the suite in the product switcher even when nothing is connected", () => {
    viewer.isAdmin = false;
    renderSidebar();
    expect(switcherRows().map((row) => row.getAttribute("href"))).toEqual([
      "https://filigran.io/platform/opencti/",
      "https://filigran.io/platform/openaev/",
      // The backend's XTM_HUB_DEFAULT_URL, as the registration status serves it.
      "https://hub.default.example.test",
    ]);
  });

  it("opens a connected platform at its own address, and never one it would not follow", () => {
    registrations = [
      { id: "r4", platform_identifier: "opencti", platform_title: "OpenCTI", platform_url: "javascript:alert(1)", healthy: true },
      { id: "r5", platform_identifier: "openaev", platform_title: "OpenAEV", platform_url: "https://aev.example.test/", healthy: true },
    ];
    hubRegistration = {
      status: "registered",
      hub_url: "https://hub.example.test",
      default_hub_url: "https://hub.default.example.test",
    };
    renderSidebar();
    expect(switcherRows().map((row) => row.getAttribute("href"))).toEqual([
      "https://filigran.io/platform/opencti/",
      "https://aev.example.test/",
      "https://hub.example.test",
    ]);
  });

  it("sends an administrator to the XTM Hub registration while the platform is not registered", () => {
    renderSidebar();
    const rows = switcherRows();
    expect(rows[rows.length - 1]).toHaveAttribute("href", "/settings/platform");
  });

  it("draws each product's own wordmark on its row", () => {
    // The option's logo is its only visible content: the wordmarks OpenCTI and
    // OpenAEV draw their own switchers with, never a row left blank.
    renderSidebar();
    const logos = switcherRows().map((row) => row.closest("[role=menuitem]")?.querySelector("img")?.getAttribute("src") ?? "");
    expect(logos).toHaveLength(3);
    expect(logos[0]).toMatch(/opencti/);
    expect(logos[1]).toMatch(/openaev/);
    expect(logos[2]).toMatch(/xtm-hub/);
  });

  it("draws OpenCRQ's own wordmark on its row, never its glyph and a typed name", () => {
    // Sam, 21 Nov 2026: the OpenCRQ row was a white glyph and plain text while
    // every other row is the product's full-colour wordmark.
    registrations = [
      { id: "r6", platform_identifier: "opencrq", platform_title: "OpenCRQ", platform_url: "https://crq.example.test/", healthy: true },
    ];
    renderSidebar();
    const rows = switcherRows().map((row) => row.closest("[role=menuitem]") ?? row);
    const crq = rows.find((row) => row.querySelector("img")?.getAttribute("src")?.includes("opencrq"));
    expect(crq).toBeDefined();
    // The wordmark is the row's only image, with no visible name typed beside it.
    expect(crq?.querySelectorAll("img")).toHaveLength(1);
    expect(crq?.querySelector(".content-base")).toBeNull();
  });

  it("has no product switcher in Copilot mode: the logo alone, a link home", () => {
    // Sam, 21 Nov 2026: the suite switcher is XTM One's; Copilot shows its
    // platform logo linking home, with no chevron and no menu.
    mode.xtmOne = false;
    renderSidebar("/observability/traces");
    expect(screen.queryByRole("button", { name: "Switch product" })).not.toBeInTheDocument();
    expect(homeImage()).toBe("/logo.png");
    fireEvent.click(screen.getByRole("link", { name: "XTM One home" }));
    expect(screen.getByTestId("location").textContent).toBe("/");
  });

  it("draws the mark alone in the collapsed rail in Copilot mode too", () => {
    mode.xtmOne = false;
    railState.rail = true;
    renderSidebar();
    expect(homeImage()).toBe("/emblem.svg");
  });

  it("goes home from the switcher's logo through the router, without a page load", () => {
    renderSidebar("/observability/traces");
    fireEvent.click(screen.getByRole("link", { name: "XTM One home" }));
    expect(screen.getByTestId("location").textContent).toBe("/");
  });

  it("draws the wordmark while expanded", () => {
    renderSidebar();
    expect(homeImage()).toBe("/logo.png");
  });

  // The rail is 48px wide: the wordmark squeezed into it shrank to an
  // unreadable strip, which is what every Copilot deployment showed.
  it("draws the product mark alone in the collapsed rail", () => {
    railState.rail = true;
    renderSidebar();
    expect(homeImage()).toBe("/emblem.svg");
    // Collapsed, the switcher is the home link alone: there is no menu to open.
    expect(screen.queryByRole("button", { name: "Switch product" })).not.toBeInTheDocument();
  });

  it("crops the logo in the rail when the platform has no mark of its own", () => {
    branding.emblem = null;
    railState.rail = true;
    renderSidebar();
    expect(homeImage()).toBe("/logo.png");
  });

  it("words a locked row of a group as its label then its reason, the text its tooltip shows", () => {
    // #10292 UX r1 B1: the library's row tooltip reads the row's text, which
    // ran the label into the reason ("IntentsAI is disabled...").
    lockState.aiReason = "AI is disabled on this platform.";
    renderSidebar();
    const intents = screen.getByRole("link", { name: /^Intents/ });
    expect(intents).toHaveAttribute("href", "/intents");
    expect(intents.textContent).toBe("Intents: AI is disabled on this platform.");
  });

  it("gives a locked row of a group one tab stop: the link, with no focusable or disabled descendant", () => {
    // #10292 UX r1 B2: a tooltip wrapper inside the link was a second tab stop
    // marked aria-disabled on a row that stays a working link.
    lockState.aiReason = "AI is disabled on this platform.";
    renderSidebar();
    const intents = screen.getByRole("link", { name: /^Intents/ });
    expect(intents.querySelectorAll("[tabindex], a, button, input, select, textarea")).toHaveLength(0);
    expect(intents.querySelector("[aria-disabled]")).toBeNull();
    expect(intents).not.toHaveAttribute("aria-disabled");
  });

  it("gives a locked group heading one tab stop, named by its label then its reason", () => {
    // #10292 UX r2 B1: a tooltip wrapper inside the heading's trigger was a
    // second, aria-disabled tab stop, and the reason showed on its focus only.
    lockState.aiReason = "AI is disabled on this platform.";
    const studio = sections[1].entries[0];
    if (studio.kind !== "group") throw new Error("fixture: Agent Studio is a group");
    studio.locked = true;
    studio.lockReason = "ai_disabled";
    try {
      renderSidebar();
      const heading = screen.getByRole("button", { name: /^Agent Studio/ });
      expect(heading.textContent).toBe("Agent Studio: AI is disabled on this platform.");
      expect(heading.querySelectorAll("[tabindex], a, button, input, select, textarea")).toHaveLength(0);
      expect(heading.querySelector("[aria-disabled]")).toBeNull();
    } finally {
      studio.locked = undefined;
      studio.lockReason = undefined;
    }
  });

  it("keeps each rail row's label as its name, without a title attribute", () => {
    // The rail hides labels visually only; a row that dropped its text named
    // itself through `title`, which the library's tooltip cannot read.
    railState.rail = true;
    renderSidebar();
    const home = screen.getByRole("link", { name: "Home" });
    expect(home).not.toHaveAttribute("title");
    expect(within(home).getByText("Home")).toBeInTheDocument();
  });

  it("shows the full label of a cut row of a group or a section in a tooltip, on focus as on hover", async () => {
    // #10450 UX r1 B1: "Utilisateurs et groupes" is cut in the rail width.
    // jsdom lays nothing out, so every box reads as narrower than its text.
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(124);
    const client = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(114);
    try {
      renderSidebar();
      for (const name of ["Knowledge", "Traces"]) {
        const row = screen.getByRole("link", { name });
        act(() => row.focus());
        expect(await screen.findByRole("tooltip")).toHaveTextContent(name);
        act(() => row.blur());
      }
      // A cut group heading too ("Tools & Integrations" in 104 px, fr "Outils
      // et intégrations" 135 px; #11469 UX r1 A4, #11471 UX r2 A1).
      const heading = screen.getByRole("button", { name: "Agent Studio" });
      act(() => heading.focus());
      expect(await screen.findByRole("tooltip")).toHaveTextContent("Agent Studio");
      act(() => heading.blur());
      // Hover too, through the pointer events Radix reads (opens after its delay).
      const knowledge = screen.getByRole("link", { name: "Knowledge" });
      fireEvent.pointerMove(knowledge, { pointerType: "mouse" });
      expect(await screen.findByRole("tooltip", {}, { timeout: 2000 })).toHaveTextContent("Knowledge");
    } finally {
      scroll.mockRestore();
      client.mockRestore();
    }
  });

  it("names a group heading by its label alone, with no native title tooltip", () => {
    // #10338 UX r1 B1: a title on the heading label stacked an OS tooltip on
    // the library one that already shows a cut label in full.
    renderSidebar();
    const heading = screen.getByRole("button", { name: /^Agent Studio/ });
    const label = within(heading).getByText("Agent Studio");
    expect(label).not.toHaveAttribute("title");
    expect(heading.querySelector("[title]")).toBeNull();
  });

  it.each([
    ["expanded", false],
    ["collapsed", true],
  ])("ends with Made by Filigran and the collapse toggle while %s, as OpenCTI and OpenAEV do", (_label, rail) => {
    railState.rail = rail;
    renderSidebar();
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    const filigran = within(nav).getByRole("link", { name: "Made by Filigran" });
    expect(filigran).toHaveAttribute("href", "https://filigran.io/");
    expect(filigran).toHaveAttribute("target", "_blank");
    expect(within(nav).getByRole("button", { name: rail ? "Expand" : "Collapse" })).toBeInTheDocument();
  });
});

describe("Sidebar collapsed to the rail", () => {
  beforeEach(() => {
    railState.rail = true;
  });

  /** Past the library's hover-close delay (150 ms). */
  const outlastHoverClose = () => act(() => new Promise((resolve) => setTimeout(resolve, 300)));

  /** Points at a rail row, the way the pointer arrives from the page. */
  async function openFlyout(name: string) {
    const trigger = screen.getByRole("link", { name });
    fireEvent.mouseEnter(trigger);
    return { trigger, menu: await screen.findByRole("menu") };
  }

  it("opens no flyout on load, not even for the expanded group of the current page", () => {
    renderSidebar("/knowledge");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(expandedGroups()).toEqual(SEEDED_EXPANDED);
  });

  it("keeps a flyout open while the pointer moves into it, and navigates from its entries", async () => {
    renderSidebar("/");
    const { trigger, menu } = await openFlyout("Agent Studio");
    // One mouseout from the icon into the panel: React dispatches the trigger's
    // leave and the panel's enter from it, as for a real pointer.
    fireEvent.mouseOut(trigger, { relatedTarget: menu });
    fireEvent.mouseOver(menu, { relatedTarget: trigger });
    await outlastHoverClose();
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Knowledge" }));
    expect(screen.getByTestId("location").textContent).toBe("/knowledge");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("closes a flyout the pointer leaves for the page", async () => {
    renderSidebar("/");
    const { menu } = await openFlyout("Agent Studio");
    fireEvent.mouseOut(menu, { relatedTarget: document.body });
    await outlastHoverClose();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("leaves the groups the full-width sidebar shows expanded alone", async () => {
    renderSidebar("/");
    const { trigger, menu } = await openFlyout("Agent Studio");
    fireEvent.mouseOut(trigger, { relatedTarget: menu });
    fireEvent.mouseOut(menu, { relatedTarget: document.body });
    await outlastHoverClose();
    expect(expandedGroups()).toEqual(SEEDED_EXPANDED);
  });
});
