// SPDX-License-Identifier: Apache-2.0

/**
 * Home and command centre.
 */
import { LayoutDashboard } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "dashboard.open",
    category: "Navigation",
    title: "Open the dashboard",
    icon: LayoutDashboard,
    navigate: "/dashboard",
  },
]);
