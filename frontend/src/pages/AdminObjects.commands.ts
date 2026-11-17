// SPDX-License-Identifier: Apache-2.0

/**
 * Remaining admin surfaces that have no action beyond being opened.
 */
import { Boxes, ScrollText, TrendingUp, Zap } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "admin.objects",
    category: "Admin",
    title: "Browse objects",
    icon: Boxes,
    when: ["isAdmin"],
    navigate: "/objects",
  },
  {
    id: "admin.logs",
    category: "Admin",
    title: "Open audit logs",
    icon: ScrollText,
    when: ["isAdmin"],
    navigate: "/logs",
  },
  {
    id: "admin.aiAdoption",
    category: "Admin",
    title: "Open AI adoption",
    icon: TrendingUp,
    when: ["isAdmin", "aiAdoption"],
    navigate: "/ai-adoption",
  },
  {
    id: "admin.intents",
    category: "Admin",
    title: "Browse intents",
    icon: Zap,
    navigate: "/intents",
  },
]);
