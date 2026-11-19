// SPDX-License-Identifier: Apache-2.0

/**
 * The description of a "Download failed" toast.
 *
 * A failed download used to put the backend's raw reason in the toast
 * ("Impossible de télécharger « report.pdf » : File not found"): English inside
 * a translated sentence, and the whole description when the file had no name.
 * Every download toast (a `file:` link in a chat answer, an attachment chip, a
 * file card, a newer revision, a knowledge base export package, an MCP
 * installer package) now maps its error here: a 404 says the file no longer
 * exists, a 403 that the reader has no access to it, anything else is a
 * generic translated failure. The raw reason goes to the console only.
 *
 * Kept out of the component files so they export components only
 * (`react-refresh/only-export-components`).
 */

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** The HTTP status a download error carries (`api.download`, `authenticatedDownload`). */
export function downloadErrorStatus(error: unknown): number | undefined {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === "number" ? status : undefined;
}

/**
 * Why a `410 Gone` download's file went (`deleted_reason` of the backend's
 * body, read from the error's `detail` or `body`), `undefined` when unsaid.
 */
function downloadErrorReason(error: unknown): string | undefined {
  const holder = error as { detail?: unknown; body?: unknown; data?: unknown } | null;
  for (const candidate of [holder?.detail, holder?.body, holder?.data]) {
    const inner = (candidate as { detail?: unknown } | null)?.detail ?? candidate;
    const reason = (inner as { deleted_reason?: unknown } | null)?.deleted_reason;
    if (typeof reason === "string" && reason) return reason;
  }
  return undefined;
}

/**
 * The translated toast description for a failed download of `filename`
 * (the file's name or the link's text; empty when there is none).
 */
export function downloadErrorDescription(
  error: unknown,
  filename: string | null | undefined,
  t_i18n: Translate,
): string {
  // The backend's reason stays out of the toast; it is kept for whoever debugs.
  console.warn("download failed", error);
  const name = filename?.trim() ?? "";
  const status = downloadErrorStatus(error);
  // 410 Gone (#396): the file's content was deleted (a retention, its owner, an
  // administrator); the body's `deleted_reason` says which.
  if (status === 410) {
    const reason = downloadErrorReason(error);
    if (reason === "admin") {
      return name
        ? t_i18n("\"{filename}\" was deleted by an administrator.", { filename: name })
        : t_i18n("This file was deleted by an administrator.");
    }
    if (reason === "owner_deleted") {
      return name
        ? t_i18n("\"{filename}\" was deleted by its owner.", { filename: name })
        : t_i18n("This file was deleted by its owner.");
    }
    return name
      ? t_i18n("\"{filename}\" was deleted by the retention policy.", { filename: name })
      : t_i18n("This file was deleted by the retention policy.");
  }
  if (status === 404) {
    return name
      ? t_i18n("\"{filename}\" no longer exists.", { filename: name })
      : t_i18n("This file no longer exists.");
  }
  if (status === 403) {
    return name
      ? t_i18n("You do not have access to \"{filename}\".", { filename: name })
      : t_i18n("You do not have access to this file.");
  }
  return name
    ? t_i18n("Could not download \"{filename}\". Please try again.", { filename: name })
    : t_i18n("Could not download this file. Please try again.");
}
