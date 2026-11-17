// SPDX-License-Identifier: Apache-2.0

/** The catalog key of each platform role the backend writes. @i18n-keys */
const ROLE_LABELS = {
  admin: "Admin",
  user: "User",
} as const;

/**
 * The catalog key to show for a user's platform role: the known ones
 * translated, an unknown one (a role a newer backend added) shown as written.
 * Used by the Users list, the user page and the group members list so the
 * three show the same word for the same role.
 */
export function roleLabelKey(role: string): string {
  return (ROLE_LABELS as Record<string, string>)[role] ?? role;
}
