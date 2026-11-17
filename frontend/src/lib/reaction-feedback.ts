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
 * Reactions as feedback: what the setting editor needs to know about emojis.
 *
 * The server owns the rule (`backend/app/services/reaction_feedback.py`) and
 * validates every setting; this mirrors its spelling of an emoji - the skin
 * tones and presentation selectors dropped - so the editor compares what it
 * shows with what the server stores, and the limits it enforces.
 */
import type { ReactionFeedbackSetting } from "@/types";

/** Emojis one sign may hold (`MAX_EMOJIS_PER_SIGN`). */
export const MAX_EMOJIS_PER_SIGN = 10;

/** What the server reads when an account never set anything (`DEFAULT_CONFIG`). */
export const DEFAULT_REACTION_FEEDBACK: ReactionFeedbackSetting = {
  enabled: true,
  positive: ["\u{1F44D}"],
  negative: ["\u{1F44E}"],
};

/** The emojis the editor offers, as people react with them. */
export const REACTION_PALETTE: readonly string[] = [
  "\u{1F44D}", // thumbs up
  "\u{1F44E}", // thumbs down
  "❤️", // red heart
  "\u{1F606}", // laughing
  "\u{1F62E}", // open mouth
  "\u{1F622}", // crying
  "\u{1F621}", // pouting
  "✅", // check mark button
  "❌", // cross mark
  "\u{1F389}", // party popper
  "\u{1F44F}", // clapping hands
  "\u{1F64F}", // folded hands
  "\u{1F525}", // fire
  "\u{1F4AF}", // hundred points
  "\u{1F914}", // thinking face
  "\u{1F440}", // eyes
];

/** Presentation selectors and the five skin tones: they only change how an emoji looks. */
function isVariant(codePoint: number): boolean {
  return codePoint === 0xfe0e || codePoint === 0xfe0f || (codePoint >= 0x1f3fb && codePoint <= 0x1f3ff);
}

/** One spelling per emoji, the server's: a thumbs up of any skin tone is the thumbs up. */
export function normalizeEmoji(value: string): string {
  return Array.from(value.normalize("NFC"))
    .filter((ch) => !isVariant(ch.codePointAt(0) ?? 0))
    .join("")
    .trim();
}

export type ReactionSign = "positive" | "negative" | null;

/** What *emoji* reads as in *setting*. */
export function signOf(setting: ReactionFeedbackSetting, emoji: string): ReactionSign {
  const key = normalizeEmoji(emoji);
  if (setting.positive.some((e) => normalizeEmoji(e) === key)) return "positive";
  if (setting.negative.some((e) => normalizeEmoji(e) === key)) return "negative";
  return null;
}

/** The next sign of an emoji the editor cycles through: none, positive, negative, none. */
export function nextSign(sign: ReactionSign): ReactionSign {
  if (sign === null) return "positive";
  if (sign === "positive") return "negative";
  return null;
}

/**
 * *setting* with *emoji* moved to *sign* (removed from the other one). A sign
 * already full keeps the emoji out of it: the server would refuse the setting.
 */
export function withSign(
  setting: ReactionFeedbackSetting,
  emoji: string,
  sign: ReactionSign,
): ReactionFeedbackSetting {
  const key = normalizeEmoji(emoji);
  const without = (list: string[]) => list.filter((e) => normalizeEmoji(e) !== key);
  const positive = without(setting.positive);
  const negative = without(setting.negative);
  if (sign === "positive" && positive.length < MAX_EMOJIS_PER_SIGN) positive.push(key);
  if (sign === "negative" && negative.length < MAX_EMOJIS_PER_SIGN) negative.push(key);
  return { ...setting, positive, negative };
}

/** The palette, then the emojis of *setting* the palette does not show. */
export function paletteFor(setting: ReactionFeedbackSetting): string[] {
  const shown = REACTION_PALETTE.map(normalizeEmoji);
  const extra = [...setting.positive, ...setting.negative]
    .map(normalizeEmoji)
    .filter((e, i, all) => e && !shown.includes(e) && all.indexOf(e) === i);
  return [...REACTION_PALETTE, ...extra];
}
