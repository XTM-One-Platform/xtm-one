// SPDX-License-Identifier: Apache-2.0

/**
 * Integrations.
 */
import { Link2, Plus } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "integrations.create",
    category: "Integrations",
    title: "Connect an integration",
    keywords: ["oauth", "credentials", "provider"],
    icon: Plus,
    navigate: "/integrations/new",
  },
  {
    id: "integrations.list",
    category: "Integrations",
    title: "Browse integrations",
    icon: Link2,
    navigate: "/integrations",
  },
]);
