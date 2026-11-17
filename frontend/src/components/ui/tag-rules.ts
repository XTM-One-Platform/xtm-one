// SPDX-License-Identifier: Apache-2.0

/** The text rules `TagInput` applies, free of React so `tag-rules.test.ts`
 *  runs without a DOM. The field itself is tested by
 *  `tests_e2e/dataset-label-vocabulary.spec.ts`.
 */

/** What `TagInput` stores for a piece of typed text.
 *
 *  A free-form tag field folds case, so `Malware` and `malware` stay one tag.
 *  A label vocabulary keeps it: the author reads back `T1003.001` as declared, while
 *  scoring and `validate_reference_labels` compare labels case-folded.
 */
export function normalizeTag(raw: string, preserveCase = false): string {
  const trimmed = raw.trim();
  return preserveCase ? trimmed : trimmed.toLowerCase();
}

/** Whether `tag` is already in `existing`, ignoring case in BOTH modes:
 *  `t1003` beside `T1003` is a mistyping, never a second label.
 */
export function containsTag(existing: readonly string[], tag: string): boolean {
  const folded = tag.toLowerCase();
  return existing.some((candidate) => candidate.toLowerCase() === folded);
}


/** The platform tags to offer for `input`, or none at all.
 *
 *  The gate lives in the data, not the view: `enabled: false` stops the
 *  fetch but still returns whatever the `["tags"]` cache holds, and a non-empty list
 *  lets ArrowDown + Enter store a lowercase platform tag in place of what was typed.
 *
 *  Matching is case-insensitive whatever the field stores: that is search, not storage.
 */
export function platformSuggestions(
  allTags: readonly string[],
  selected: readonly string[],
  input: string,
  suggestFromPlatformTags: boolean,
  limit = 50,
): string[] {
  if (!suggestFromPlatformTags) return [];
  const needle = input.trim().toLowerCase();
  return allTags
    .filter((tag) => !containsTag(selected, tag))
    .filter((tag) => (needle ? tag.toLowerCase().includes(needle) : true))
    .slice(0, limit);
}
