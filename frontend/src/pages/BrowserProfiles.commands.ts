/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Browser profiles: saved sign-ins the agents' browser opens.
 */
import { KeyRound, Plus } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "browserProfiles.list",
    category: "Browser profiles",
    title: "Browse browser profiles",
    keywords: ["sign-in", "login", "session", "credentials", "mfa"],
    icon: KeyRound,
    navigate: "/connections",
  },
  {
    id: "browserProfiles.create",
    category: "Browser profiles",
    title: "New browser profile",
    keywords: ["sign-in", "login", "session", "credentials"],
    icon: Plus,
    navigate: "/connections?new=1",
  },
]);
