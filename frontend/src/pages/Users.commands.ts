// SPDX-License-Identifier: Apache-2.0

/**
 * User administration.
 */
import { User, UserPlus } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "users.create",
    category: "Users",
    title: "Invite a user",
    keywords: ["new", "account", "invitation"],
    icon: UserPlus,
    when: ["isAdmin"],
    navigate: "/users-and-groups/users/new",
  },
  {
    id: "users.list",
    category: "Users",
    title: "Browse users",
    icon: User,
    when: ["isAdmin"],
    navigate: "/users-and-groups",
  },
]);
