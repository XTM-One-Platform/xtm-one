// SPDX-License-Identifier: Apache-2.0

/**
 * Which registration token the connect instructions may embed.
 *
 * Only a token of the operator's own is ever handed out as the value to paste
 * into OpenCTI / OpenAEV / OpenCRQ: the published docker-compose fallback
 * authenticates nobody and cannot connect a new platform, and an unset token
 * disables registration. Anything else - loading, a non-admin, an error - is
 * the neutral placeholder.
 */
import type { RegistrationTokenInfo } from "@/types";

export const REGISTRATION_TOKEN_PLACEHOLDER = "<registration-token>";

export function connectSnippetToken(info: RegistrationTokenInfo | undefined): string {
  return info?.state === "configured" && info.registration_token
    ? info.registration_token
    : REGISTRATION_TOKEN_PLACEHOLDER;
}
