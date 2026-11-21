// SPDX-License-Identifier: Apache-2.0

/**
 * Declarative model of the left navigation.
 *
 * The tree is data, not JSX, because two surfaces consume it: the `Sidebar`
 * (renders it) and the ⌘K command palette (flattens it into destinations).
 * Keeping one source of truth means a new page is added to both at once. The
 * hub pages (`/agents-hub`, `/work`, `/agent-studio`, `/tools-and-integrations`)
 * build their cards from their resolved group (`use-nav-group.ts`, #9301 N2c),
 * so a new page in a hub's group shows on its hub too, and its hub's test
 * fails until the page's card data is added there.
 *
 * Depth is capped at two levels by construction: a section holds leaves or
 * groups, and a group holds leaves only. Anything deeper belongs in page tabs
 * (see `detail-tabs.tsx`), not in the nav.
 *
 * The top block reads as a sentence — Dashboard, Chat, the **Orchestration** group
 * (the agents, the fleets they form, the flows between them, the intents that
 * route to them), then the **Work** group holding what those agents produce
 * (runs, their value, decks, files). The block underneath is what agents are *made of* (Agent Studio)
 * and what they *act through* (Tools & Integrations). A page is placed by
 * that question — made of / acts through / produced — and never promoted to
 * the top level just because it is visited often: the top block is the story,
 * not a most-used list, and every extra top-level entry dilutes it.
 *
 * Blocks are separated by a rule and carry no heading, as in OpenCTI and
 * OpenAEV: a section's `label` is shown only as the heading row of a
 * collapsible section.
 */
import {
  Activity, BarChart3, BookOpen, Bot, Boxes, Braces, Briefcase, Cable, Clapperboard, ClipboardList, Compass,
  Database, FileText, FlaskConical, FolderOpen, Gauge, GraduationCap, KeyRound, LayoutDashboard, LayoutGrid,
  LayoutTemplate, Link2, ListChecks, MessageSquare, Network, Presentation, Puzzle, Route, Scan, ScrollText,
  Server, Settings, Shield, Sparkles, Sprout, Target, Telescope, User, Users, Wand2, Workflow,
  Wrench, Zap, type LucideIcon,
} from "lucide-react";

/** Platform/feature flags a nav entry can depend on, resolved once per render. */
export interface NavFlags {
  chat: boolean;
  customVariables: boolean;
  /** On-premises relays (`relays_enabled`). */
  relays: boolean;
  xtmOne: boolean;
  aiAdoption: boolean;
  /** Whether AI is switched on. Drives `locked`, never removal. */
  aiEnabled: boolean;
  /** Whether AI is LICENSED — Enterprise Edition, or a valid license in
   *  Copilot mode. Drives `locked` too: a Community Edition deployment lists
   *  every AI area and marks it, so the upgrade path is one hover away
   *  instead of a section that quietly does nothing. */
  aiLicensed: boolean;
  /** `DEEP_INVESTIGATION`: the investigation packs live behind it. */
  deepInvestigation: boolean;
  /** `VIDEO_STUDIO`: the Videos page lives behind it. */
  videoStudio: boolean;
  /** `DASHBOARD_STUDIO`: the Dashboards pages live behind it. */
  dashboardStudio: boolean;
  /** `EVALUATION`: served only where evaluation may run (every mode where AI runs). */
  evaluation: boolean;
  /** The Platform Health page (#8261: improvements, cycles, signals); built in since #101 (S1), no flag, so callers pass true. */
  health: boolean;
  /** Intents: agent routing for requests of the XTM platforms (OpenCTI,
   *  OpenAEV), XTM One domain content (dev-docs/mode-split.md). The callers set
   *  it from the mode: on in XTM One mode (AI on or off), off in Copilot mode,
   *  where nothing routes through an intent (Sam, 19 Nov 2026, #445). */
  intents: boolean;
}

/** Why a `locked` entry is locked — picks the tooltip that explains it. */
type NavLockReason = "ai_disabled" | "unlicensed";

export interface NavLeaf {
  kind: "leaf";
  to: string;
  icon: LucideIcon;
  label: string;
  /** Hidden when this returns false. Omitted = always visible. */
  visible?: (flags: NavFlags) => boolean;
  /**
   * Declares the entry as an AI area. Deliberately **not** wired to
   * `visible`: FR-017 wants these listed and reachable, marked unavailable —
   * `visible` removes them, which is the opposite while looking like the
   * natural fit. Set `requiresAi` and the resolver sets `locked` instead.
   */
  requiresAi?: boolean;
  /**
   * A read surface of Enterprise Edition that keeps working with AI switched
   * off (the Value page reads past runs, #9836 R5): locked on Community
   * Edition only, with the upgrade tooltip.
   */
  requiresLicense?: boolean;
  /**
   * Resolved by `resolveNav`, not declared: `requiresAi` and AI is off or not
   * licensed. The entry still renders and still navigates — the read surface
   * is the whole point of both states (FR-008, SC-006) — it is only marked
   * unavailable.
   */
  locked?: boolean;
  /** Resolved with `locked`. The administrator's switch wins over the licence:
   *  no licence enables AI that is switched off (FR-031). */
  lockReason?: NavLockReason;
  /** In an `adminOnly` section: listed for a group manager too, whose
   *  team the page reads. The only entries of that section they see. */
  groupManagers?: boolean;
  /** A name shown as written, never a message id (none since #101 S1: Platform Health is a translated label). */
  text?: string;
  /** Listed for administrators only, inside a section every user sees (Intents
   *  in the Orchestration group, #9301). A group manager does not get it either. */
  adminOnly?: boolean;
  /** Other pages that light this row (longest prefix still wins): the home
   *  row lights on the command center routes too (#9301, C2). */
  alsoActive?: string[];
  /**
   * A page family (#9301 N3): one row for several pages that read as one
   * subject (Observability: Model usage, Outcomes, Traces, Sessions). The row
   * opens its first visible member and lights on every member; each member
   * page renders the family h1 and ONE tab row of the members
   * (`NavFamilyHeader`). Resolved per reader: a reader who sees one member
   * only gets a plain row with that member's label and route, no tab row.
   */
  family?: NavFamilyMember[];
}

/** One page of a `NavLeaf.family`. */
interface NavFamilyMember {
  to: string;
  label: string;
  icon: LucideIcon;
  visible?: (flags: NavFlags) => boolean;
  /** In an `adminOnly` section: a group manager reads this member too. */
  groupManagers?: boolean;
}

export interface NavGroup {
  kind: "group";
  /** Stable id — also the persistence key for the expanded/collapsed state. */
  id: string;
  label: string;
  icon: LucideIcon;
  /**
   * Hub page. When set, the group label is a real destination (clicking it
   * opens the hub *and* expands the group) and the chevron is the only
   * expand/collapse affordance. When unset the whole row just toggles.
   */
  to?: string;
  /** One-liner, shown under the hub page title. */
  description?: string;
  /** Other words the ⌘K palette finds the group by (English, like the
   *  label): the former name of a renamed group, so muscle memory still
   *  lands (Orchestration answers "Agents", #9301 N2d). */
  keywords?: string[];
  children: NavLeaf[];
  /** As `NavLeaf.requiresAi`, for the group's own row (its hub page) only:
   *  each child carries its own lock, so a mixed group (Orchestration: an AI hub,
   *  Agentic Flow open, Fleets licence-locked) marks every row truly. */
  requiresAi?: boolean;
  /** Destinations of the ⌘K palette with no sidebar row: an action reached
   *  from the group's pages (Create an agent, a button on the Agents list and
   *  the Agent Studio hub, #9301 UX r1 B1). Locked like a row. */
  paletteOnly?: NavLeaf[];
  /** Resolved by `resolveNav`. See `NavLeaf.locked`. */
  locked?: boolean;
  /** Resolved with `locked`. See `NavLeaf.lockReason`. */
  lockReason?: NavLockReason;
}

type NavEntry = NavLeaf | NavGroup;

export interface NavSection {
  /** Stable id — persistence key when `collapsible`. */
  id: string;
  /** The heading row of a collapsible section, and the ⌘K palette's
   *  secondary line for its entries. A plain block has none. */
  label?: string;
  /** When true the section label itself expands/collapses its entries. */
  collapsible?: boolean;
  /** Required when `collapsible` — the rail has no room for the label, so the
   *  section is represented by this icon and a hover flyout. */
  icon?: LucideIcon;
  adminOnly?: boolean;
  entries: NavEntry[];
}

const leaf = (
  to: string,
  icon: LucideIcon,
  label: string,
  visible?: (flags: NavFlags) => boolean,
): NavLeaf => ({ kind: "leaf", to, icon, label, visible });

/** An AI area: listed and reachable with AI off, but marked unavailable. */
const aiLeaf = (
  to: string,
  icon: LucideIcon,
  label: string,
  visible?: (flags: NavFlags) => boolean,
): NavLeaf => ({ kind: "leaf", to, icon, label, visible, requiresAi: true });

export const NAV_SECTIONS: NavSection[] = [
  {
    id: "primary",
    entries: [
      /* The command center pages (C2 of the program) light the home row: they
         are the home, reached from it. Named "Home" (#9301 Q3), so the studio
         keeps "Dashboards" with no name collision. */
      { ...leaf("/", LayoutDashboard, "Home"), alsoActive: ["/dashboard", "/briefing", "/ctem"] },
      aiLeaf("/chat", MessageSquare, "Chat", (f) => f.chat),
      /* The agents and what they form and follow (#9301): the label opens a
         hub of cards, like Agent Studio and Tools & Integrations (N2c), and
         the list is a child row of its own. The group row carries no lock
         (a mixed group: Agentic Flow stays open); each child carries its own.
         Named Orchestration (N2d, Sam 9 Nov 2026): a group never repeats one
         of its rows, and "Workforce" read too close to Work. The id and the
         hub path stay: the id keys the expanded state, the path old links. */
      {
        kind: "group",
        id: "agents",
        label: "Orchestration",
        icon: Workflow,
        to: "/agents-hub",
        description: "Your agents, the fleets they form and the flows they follow.",
        keywords: ["Agents"],
        children: [
          aiLeaf("/agents", Bot, "Agents"),
          /* Its own page since N2c (it was a tab of Agentic Flow, so two rows
             opened one page). Licence-locked only: with AI off the fleets stay
             readable, as `FleetsTab.tsx` says. */
          { ...leaf("/fleets", Boxes, "Fleets"), requiresLicense: true },
          /* NOT gated on `FLOW_BUILDER`. The page's agent-to-agent map (and its
             schedule view) predates the visual builder and is independent of it
             (agents, assignments and delegations — no flow endpoint between them).
             Only the `Flows` library tab is hidden, by `Flows.tsx` itself. */
          leaf("/flows", Route, "Agentic Flow"),
          /* Agent routing, next to the agents it routes to; administrators only,
             as it was in Admin, and XTM One mode only (#445). */
          { ...aiLeaf("/intents", Zap, "Intents", (f) => f.intents), adminOnly: true },
        ],
        /* The existing full-page creation wizard: a button on the Agents list
           and the Agent Studio hub, and a palette destination here. */
        paletteOnly: [aiLeaf("/agents/new", Wand2, "Create an agent")],
      },
      /* What the agents above PRODUCE — the counterpart of the two groups of
         the next block (made of / acts through). One group rather than three
         top-level entries: each of these is a destination you go to for what
         an agent did, not a thing you configure, and listing them singly next
         to Chat and Orchestration turned the top block into a flat most-used list.
         Decks stay here rather than under Agent Studio (which holds what
         agents are made of), and are not gated on `chat`: an assignment run
         generates a deck too. Operations and Files are plain leaves —
         a read surface over work already done keeps working with AI off. */
      {
        kind: "group",
        id: "work",
        label: "Work",
        icon: Briefcase,
        to: "/work",
        description: "What your agents produce: runs, decks, and files.",
        children: [
          /* Tasks (#775, Sam, 20 Nov 2026): what to do and follow (Work Items,
             Value, Workspaces), right above what is running or past
             (Operations). Value is a tab of Tasks, no row of its own: its
             Community Edition upgrade path is drawn in the tab. Every user,
             both modes, open with AI off. */
          /* "Tasks page": the id "Tasks" is the A2A edge's verb ("Confie", "Asigna"). */
          leaf("/tasks", ListChecks, "Tasks page"),
          leaf("/operations", ClipboardList, "Operations"),
          aiLeaf("/presentations", Presentation, "Presentations"),
          aiLeaf("/videos", Clapperboard, "Videos", (f) => f.videoStudio),
          leaf("/dashboards", LayoutGrid, "Dashboards", (f) => f.dashboardStudio),
          leaf("/files", FolderOpen, "Files"),
        ],
      },
    ],
  },
  {
    id: "resources",
    entries: [
      {
        kind: "group",
        id: "agent-studio",
        label: "Agent Studio",
        icon: Sparkles,
        to: "/agent-studio",
        description: "Everything that defines what your agents know, say, and can do.",
        requiresAi: true,
        children: [
          aiLeaf("/knowledge", BookOpen, "Knowledge"),
          aiLeaf("/prompts", FileText, "Prompts"),
          aiLeaf("/templates", LayoutTemplate, "Templates"),
          aiLeaf("/skills", GraduationCap, "Skills"),
          aiLeaf("/investigation-packs", Telescope, "Investigation packs", (f) => f.deepInvestigation),
        ],
      },
      {
        kind: "group",
        id: "tools-and-integrations",
        label: "Tools & Integrations",
        icon: Puzzle,
        to: "/tools-and-integrations",
        description: "Capabilities and connections your agents act through.",
        children: [
          /* First in the group: an MCP server is the richest capability an
             agent acts through (a whole tool server, not one function), and
             the page also carries the MCP endpoints this platform *exposes*.
             That second half once argued for a top-level entry; it is still
             a capability surface, so it lives here, ahead of the rest. */
          aiLeaf("/mcp-servers", Server, "MCP Servers"),
          /* Custom tools are sandboxed scripts, not AI: authoring them
             must work with AI off, because a deterministic flow
             (``action.script``) runs them. Only deleting is refused. */
          leaf("/tools", Wrench, "Tools"),
          /* The one entry for every integration. An integration's own pages
             (an imported spec, Nova's rules, OpenRouter's console) are tabs
             of its detail page, never leaves here: the nav is fixed
             structure and never grows with user data. */
          leaf("/integrations", Link2, "Integrations"),
          /* Saved sign-ins to web applications the agents' browser opens:
             a connection like an integration, kept usable with AI
             off (signing in again, answering an MFA prompt). */
          /* One family row (#9301 N3b): browser profiles, variables and relays
             are the connection infrastructure the capabilities above use, one
             subject with one tab row; each page keeps its route. Relays are the
             network path integrations, MCP servers and flow HTTP steps can be
             routed through: infrastructure, never AI-locked. A reader with the
             two flags off sees the plain row Browser profiles. */
          {
            ...leaf("/browser-profiles", Cable, "Connections"),
            family: [
              { to: "/browser-profiles", icon: KeyRound, label: "Browser profiles" },
              { to: "/variables", icon: Braces, label: "Variables", visible: (f) => f.customVariables },
              { to: "/relays", icon: Network, label: "Relays", visible: (f) => f.relays },
            ],
          },
        ],
      },
    ],
  },
  {
    /* Id kept: it is the persistence key of the section's open state
       (`copilot-nav:expanded`), so the rename keeps a user's choice. */
    id: "admin",
    label: "Administration",
    collapsible: true,
    icon: Shield,
    adminOnly: true,
    entries: [
      /* One family row (#9301 N3, UX r2 B1): the Analytics tabs are flattened
         into it, so its pages render one tab row. NOT an AI area: observability
         over AI activity that already happened is a read surface (FR-008, US4)
         with no gated action; a lock on a page that then works teaches the
         reader the lock means nothing (SC-011). A group's managers read their
         team's usage on Model usage (the server narrows every read): for them
         the row is that one page, named after it (r3 A1). Traces, Sessions and
         Outcomes stay administrators'. */
      {
        ...leaf("/analytics", Gauge, "Observability"),
        groupManagers: true,
        family: [
          { to: "/analytics", icon: BarChart3, label: "Model usage", groupManagers: true },
          { to: "/analytics/outcomes", icon: Target, label: "Outcomes" },
          { to: "/traces", icon: Scan, label: "Traces" },
          { to: "/trace-sessions", icon: Activity, label: "Sessions" },
        ],
      },
      /* Sprout, not TrendingUp: that one is the Value tab's, and no two rows share an icon. */
      aiLeaf("/ai-adoption", Sprout, "AI Adoption", (f) => f.aiAdoption),
      aiLeaf("/evaluations", FlaskConical, "Evaluations", (f) => f.evaluation),
      /* Platform Health (#101 S1): a translated label, never the administrator's name; `/concierge` redirects here. */
      aiLeaf("/health", Compass, "Platform Health", (f) => f.health),
      /* One family row (#9301 N3b): one subject, two pages, routes and route
         guards unchanged (/users behind AdminRoute, /groups not; r2 A5). */
      {
        ...leaf("/users", Users, "Users and groups"),
        family: [
          { to: "/users", icon: User, label: "Users" },
          { to: "/groups", icon: Users, label: "Groups" },
        ],
      },
      /* Database, not Boxes: the cube is the Fleets row's (N2d). */
      leaf("/objects", Database, "Objects"),
      /* Row and h1 read the same (#9301 A1). */
      leaf("/logs", ScrollText, "Audit logs"),
      leaf("/settings", Settings, "Settings"),
    ],
  },
];

const isVisible = (leafEntry: NavLeaf, flags: NavFlags) =>
  leafEntry.visible ? leafEntry.visible(flags) : true;

/**
 * Why a `requiresAi` entry is unavailable, or `null` when it is not.
 *
 * AI off wins over the licence: no licence enables AI an administrator
 * switched off (FR-031), so pointing at the upgrade path would send them to
 * buy what they already have. Marked, never dropped (FR-017).
 */
const lockReasonFor = (entry: NavLeaf | NavGroup, flags: NavFlags): NavLockReason | null => {
  if (entry.kind === "leaf" && entry.requiresLicense) return flags.aiLicensed ? null : "unlicensed";
  if (!entry.requiresAi) return null;
  if (!flags.aiEnabled) return "ai_disabled";
  if (!flags.aiLicensed) return "unlicensed";
  return null;
};

/** What an entry reads: its name as written, else its translated label. */
export function navLabel(entry: { label: string; text?: string }, t_i18n: (id: string) => string): string {
  return entry.text ?? t_i18n(entry.label);
}

function withLock<T extends NavLeaf | NavGroup>(entry: T, flags: NavFlags): T {
  const reason = lockReasonFor(entry, flags);
  return reason ? { ...entry, locked: true, lockReason: reason } : entry;
}

/**
 * The members of a family this reader sees. None: the row goes. One: a plain
 * row with that member's label and route (a group manager's "Model usage").
 */
function withFamily(entry: NavLeaf, flags: NavFlags, readsAll: boolean): NavLeaf | null {
  if (!entry.family) return entry;
  const members = entry.family.filter((member) =>
    (member.visible ? member.visible(flags) : true) && (readsAll || member.groupManagers));
  if (members.length === 0) return null;
  if (members.length === 1) {
    const [only] = members;
    return { ...entry, to: only.to, label: only.label, icon: only.icon, family: undefined };
  }
  return { ...entry, to: members[0].to, family: members };
}

/**
 * A section with every gated entry (and emptied group) removed, and every AI
 * entry marked `locked`.
 *
 * The two are deliberately different operations: `visible` removes, `locked`
 * annotates. An AI area must survive this pass — FR-017, and both the AI-off
 * mode and Community Edition exist so that AI data stays browsable.
 */
function resolveSection(section: NavSection, flags: NavFlags, isAdmin: boolean): NavSection {
  const listed = (leafEntry: NavLeaf) => isVisible(leafEntry, flags) && (isAdmin || !leafEntry.adminOnly);
  /* A family member of an admin-only section is a group manager's only when it says so. */
  const readsAll = isAdmin || !section.adminOnly;
  const entries = section.entries
    .map((entry) => {
      if (entry.kind === "leaf") {
        if (!listed(entry)) return null;
        const resolved = withFamily(entry, flags, readsAll);
        return resolved ? withLock(resolved, flags) : null;
      }
      /* A family row inside a group (Connections) resolves as a top-level one. */
      const children = entry.children
        .filter(listed)
        .map((child) => withFamily(child, flags, readsAll))
        .filter((child): child is NavLeaf => child !== null)
        .map((child) => withLock(child, flags));
      if (children.length === 0) return null;
      const paletteOnly = entry.paletteOnly?.filter(listed).map((child) => withLock(child, flags));
      return withLock({ ...entry, children, ...(paletteOnly ? { paletteOnly } : {}) }, flags);
    })
    .filter((entry): entry is NavEntry => entry !== null);
  return { ...section, entries };
}

/** What a non-admin sees of an admin-only section: a group manager its `groupManagers` leaves, anyone else nothing. */
const forNonAdmin = (section: NavSection, isGroupManager: boolean): NavSection => ({
  ...section,
  entries: isGroupManager ? section.entries.filter((entry) => entry.kind === "leaf" && entry.groupManagers) : [],
});

/** Every visible section, admin-only ones dropped for non-admins (but the entries a group manager reads). */
export function resolveNav(flags: NavFlags, isAdmin: boolean, isGroupManager = false): NavSection[] {
  return NAV_SECTIONS
    .map((section) => (section.adminOnly && !isAdmin ? forNonAdmin(section, isGroupManager) : section))
    .map((section) => resolveSection(section, flags, isAdmin))
    .filter((section) => section.entries.length > 0);
}

/**
 * The single nav route that best matches `pathname` — the longest matching
 * prefix, so `/fleets` highlights "Fleets" rather than "Agentic Flow",
 * and `/tools-and-integrations` never lights up "Tools". A row's `alsoActive`
 * pages light that row (`/dashboard` lights the home row).
 *
 * Returns `null` for pages that have no nav entry (e.g. `/profile`).
 */
export function activeNavPath(sections: NavSection[], pathname: string): string | null {
  /* [prefix matched, row it lights]: a row's own route, then its `alsoActive` pages. */
  const candidates: [string, string][] = [];
  const add = (leafEntry: NavLeaf) => {
    candidates.push([leafEntry.to, leafEntry.to]);
    for (const other of leafEntry.alsoActive ?? []) candidates.push([other, leafEntry.to]);
    for (const member of leafEntry.family ?? []) candidates.push([member.to, leafEntry.to]);
  };
  for (const section of sections) {
    for (const entry of section.entries) {
      if (entry.kind === "leaf") add(entry);
      else {
        if (entry.to) candidates.push([entry.to, entry.to]);
        entry.children.forEach(add);
      }
    }
  }
  let best: [string, string] | null = null;
  for (const candidate of candidates) {
    const [to] = candidate;
    const matches = to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(`${to}/`);
    if (matches && (best === null || to.length > best[0].length)) best = candidate;
  }
  return best ? best[1] : null;
}

/** True when the group owns the active route (its hub or any of its children). */
export function isGroupActive(group: NavGroup, activePath: string | null): boolean {
  if (!activePath) return false;
  return group.to === activePath || group.children.some((child) => child.to === activePath);
}

/** Flat destination list for the ⌘K palette, each tagged with its parent label. */
interface NavDestination {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Section or group label, shown as the palette's secondary line. */
  parent?: string;
  /** A group's `keywords`: matched by the palette, never shown. */
  keywords?: string[];
  /** The leaf's `text`: shown as written. */
  text?: string;
  /** Carried from the resolved entry so the palette marks an AI area exactly
   *  as the sidebar does — reachable, but explained. */
  locked?: boolean;
  lockReason?: NavLockReason;
}

const lockOf = (entry: NavLeaf | NavGroup): Pick<NavDestination, "locked" | "lockReason"> =>
  entry.locked ? { locked: true, lockReason: entry.lockReason } : {};

export function navDestinations(sections: NavSection[]): NavDestination[] {
  const out: NavDestination[] = [];
  for (const section of sections) {
    for (const entry of section.entries) {
      if (entry.kind === "leaf") {
        if (entry.family) {
          /* One destination per member page, under the family's name. */
          for (const member of entry.family) {
            out.push({ to: member.to, label: member.label, icon: member.icon, parent: entry.label, ...lockOf(entry) });
          }
          continue;
        }
        out.push({ to: entry.to, label: entry.label, text: entry.text, icon: entry.icon, parent: section.label, ...lockOf(entry) });
        continue;
      }
      if (entry.to) {
        out.push({ to: entry.to, label: entry.label, icon: entry.icon, parent: section.label, keywords: entry.keywords, ...lockOf(entry) });
      }
      for (const child of [...entry.children, ...(entry.paletteOnly ?? [])]) {
        if (child.family) {
          for (const member of child.family) {
            out.push({ to: member.to, label: member.label, icon: member.icon, parent: child.label, ...lockOf(child) });
          }
          continue;
        }
        out.push({ to: child.to, label: child.label, icon: child.icon, parent: entry.label, ...lockOf(child) });
      }
    }
  }
  return out;
}
