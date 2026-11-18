// SPDX-License-Identifier: Apache-2.0

import type { LLMProvider } from "@/types";

/** The chat providers when every one of them lists no model, else none: "configured, but no model", not "no provider". */
export function emptyChatProviders(providers: LLMProvider[] | undefined): LLMProvider[] {
  const chat = (providers ?? []).filter((p) => (p.capabilities ?? ["chat"]).includes("chat"));
  return chat.every((p) => !p.available_models?.length) ? chat : [];
}
