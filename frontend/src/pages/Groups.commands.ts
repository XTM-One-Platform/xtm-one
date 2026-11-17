// SPDX-License-Identifier: Apache-2.0

/**
 * Groups.
 */
import { Plus, Users } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "groups.create",
    category: "Groups",
    title: "Create a group",
    icon: Plus,
    when: ["isAdmin"],
    navigate: "/groups/new",
  },
  {
    id: "groups.list",
    category: "Groups",
    title: "Browse groups",
    icon: Users,
    navigate: "/groups",
  },
]);
