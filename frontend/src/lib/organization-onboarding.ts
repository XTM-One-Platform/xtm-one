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
 * The chat onboarding of the organization profile (#11224): once "Draft with
 * AI" saved what it could, or from "Continue onboarding" on the profile page,
 * a new chat with the person's default assistant opens and starts the
 * interview. The assistant reads where the profile stands with its
 * `organization_onboarding` tool and asks for the next missing field, so the
 * state is the profile's: a new chat started later resumes at the next field.
 */
import { primeChatDraft } from "@/lib/chat-launch";

type Translate = (text: string) => string;

/** The first message of the interview, sent on arrival: the button's own wording announced it. */
export function organizationOnboardingPrompt(t_i18n: Translate): string {
  return t_i18n(
    "Help me complete our organization profile. Check what is still missing, then ask me one question at a time, propose a value when you can infer one, save each answer I confirm, and stop when every field is filled or skipped.",
  );
}

/** Leave the interview's first message for the next new chat with the default assistant, sent on arrival. */
export function primeOrganizationOnboarding(t_i18n: Translate): void {
  primeChatDraft(organizationOnboardingPrompt(t_i18n), { autoSend: true });
}
