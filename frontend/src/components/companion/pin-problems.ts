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

/** What each refused PIN (`pinProblem` in `lib/app-lock.ts`) tells the person. @i18n-keys */
export const PIN_PROBLEMS = {
  digits: "A PIN is made of digits only.",
  length: "A PIN has {min} to {max} digits.",
  repeated: "Choose a PIN that is not one digit repeated.",
  sequence: "Choose a PIN that is not a run of consecutive digits.",
} as const;
