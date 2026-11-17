// SPDX-License-Identifier: Apache-2.0

/**
 * Typed-challenge helpers for destructive confirmations (the emergency stop).
 *
 * The user retypes a short random code shown in the dialog before the
 * confirm button enables. It is a safeguard against a slip of the mouse, not
 * a secret: the alphabet drops 0/O/1/I because the code is read off the
 * screen, and the comparison ignores case and surrounding whitespace.
 */

export const CHALLENGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
export const CHALLENGE_LENGTH = 6;

export function generateChallengeCode(length = CHALLENGE_LENGTH): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => CHALLENGE_ALPHABET[b % CHALLENGE_ALPHABET.length]).join("");
}

export function challengeMatches(code: string, typed: string): boolean {
  return code.length > 0 && typed.trim().toUpperCase() === code;
}
