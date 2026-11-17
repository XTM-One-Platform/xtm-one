// SPDX-License-Identifier: Apache-2.0

/**
 * Shell-level commands. Handlers live in `AppShell`, which owns the state they act on.
 */
import { Globe, MonitorDown, PanelLeftClose, User } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "app.nav.toggleRail",
    category: "Navigation",
    title: "Toggle the icon rail",
    keywords: ["sidebar", "collapse", "expand"],
    icon: PanelLeftClose,
    handler: "app.nav.toggleRail",
  },
  {
    /* Reachable only from the avatar menu otherwise — exactly the kind of page
       a palette should surface. */
    id: "app.profile.open",
    category: "Account",
    title: "My profile",
    icon: User,
    navigate: "/profile",
  },
  {
    id: "app.catalog.open",
    category: "Account",
    title: "Open the AI Catalog",
    icon: Globe,
    when: ["catalogEnabled"],
    navigate: "/catalog",
  },
  {
    /* The native app + the machines it enrolls. Not in the nav tree
       on purpose (the sidebar footer is its home), so the palette lists it
       here. */
    id: "app.desktop.open",
    category: "Account",
    title: "Get XTM One Desktop",
    keywords: ["desktop", "app", "download", "runner", "machine", "install"],
    icon: MonitorDown,
    when: ["runnersEnabled"],
    navigate: "/desktop",
  },
]);
