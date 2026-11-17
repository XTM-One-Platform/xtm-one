// SPDX-License-Identifier: Apache-2.0

/**
 * The credential of a Google Vertex AI provider: its service account
 * key, the JSON Google Cloud downloads, with the Vertex AI region as its
 * `location` member - what the backend's `parse_vertex_credential` reads. The
 * key stays masked in the form: only whose key it is is shown.
 */

/** The largest file the form reads as a key: a service account key is a few KB. */
export const MAX_SERVICE_ACCOUNT_KEY_BYTES = 64 * 1024;

export interface ServiceAccountSummary {
  clientEmail: string;
  projectId: string;
}

function parsedKey(text: string): Record<string, unknown> | null {
  try {
    const key: unknown = JSON.parse(text);
    return key !== null && typeof key === "object" && !Array.isArray(key) ? (key as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** The service account and project of a pasted or loaded key, or `null` while it is not one. */
export function serviceAccountSummary(text: string): ServiceAccountSummary | null {
  const key = parsedKey(text);
  if (!key || key.type !== "service_account") return null;
  const { client_email: clientEmail, project_id: projectId } = key;
  return typeof clientEmail === "string" && typeof projectId === "string" ? { clientEmail, projectId } : null;
}

/**
 * The credential sent for the provider: the key, with `location` set when a
 * region is given. Anything that is not a JSON object (a vault reference) is
 * sent as typed: the backend says what is wrong with it.
 */
export function vertexAiCredential(text: string, location: string): string {
  const region = location.trim();
  const key = region ? parsedKey(text) : null;
  return key ? JSON.stringify({ ...key, location: region }) : text.trim();
}
