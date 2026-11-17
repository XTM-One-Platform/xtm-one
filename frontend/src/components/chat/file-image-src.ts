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
 * `![alt](file:<uuid>)` in an answer: an image file of the conversation named
 * by its id.
 *
 * The tools that hand a model an image file document this form (generate_file
 * embeds `![alt](file:id)` in a PDF), and models reuse it in the chat answer
 * itself. The browser cannot load a `file:` address, so the reference is
 * rewritten to the platform's own download path, which the chat already shows
 * inline through an authenticated fetch (`AuthenticatedImage`): the reader's
 * session decides whether the file loads, never a public link.
 *
 * `[text](file:<uuid>)`, the same reference as a link, gets the same download
 * path, so the link downloads the file the way a `[[FILE:id]]` card does;
 * `ChatMarkdown`'s link renderer fetches it with the reader's session.
 *
 * Only a bare uuid is rewritten. A `file:///...` path (a local file, which the
 * Desktop app opens as a link) and any other text are left exactly as written.
 * Plain string work with no React import, so it runs under `node --test`.
 */

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";

// The alt text holds no bracket and no newline (the chat's own image
// normaliser flattens multi-line ones first); an optional "title" after the
// address is dropped, as the chat never shows it.
const FILE_SCHEME_IMAGE_RE = new RegExp(
  `!\\[([^\\]\\n]*)\\]\\(\\s*<?file:(${UUID})>?(?:\\s+"[^"\\n]*")?\\s*\\)`,
  "g",
);

// The same reference as a link: not preceded by the `!` of an image.
const FILE_SCHEME_LINK_RE = new RegExp(
  `(?<!!)\\[([^\\]\\n]*)\\]\\(\\s*<?file:(${UUID})>?(?:\\s+"[^"\\n]*")?\\s*\\)`,
  "g",
);

// The download path of a file of the conversation, as a link points at it.
const CHAT_FILE_DOWNLOAD_RE = new RegExp(`^/api/v1/chat/files/(${UUID})/download$`);

/**
 * LLMs sometimes generate markdown images with multi-line alt text that
 * contains the full generation prompt.  Standard CommonMark parsers can't
 * handle newlines inside `![...]()` — the image syntax breaks and the alt
 * text renders as literal paragraphs with a trailing link.
 *
 * This function collapses such images into a clean single-line form so the
 * parser emits a proper `<img>` node.
 *
 * Deliberately NOT the package's `normalizeImageMarkdown`: this version is
 * specific to our API and also rewrites the URL down to its
 * `/api/v1/chat/{files,images}/…` path — an absolute URL the model prepended
 * would otherwise defeat `AuthenticatedImage`'s path match, so the image
 * would render as a plain `<img>` and 401. The package's generic version only
 * flattens the alt text and never touches the URL.
 *
 * The alt text may span lines but never crosses a `](`: started at an earlier
 * image of another address (a `file:` one), a lazy alt would run across that
 * image and the prose after it to the next API image and swallow both.
 *
 * The model's alt text is kept, flattened to one line and without brackets, so
 * a screen reader still reads what the image shows; an empty alt stays
 * empty and the renderer names it in the reader's language.
 */
export function normalizeApiImageMarkdown(raw: string): string {
  return raw.replace(
    /!\[((?:(?!\]\()[\s\S])*?)\]\(((?:https?:\/\/[^)]*)?\/api\/v1\/chat\/(?:files|images)\/[^)]+)\)/g,
    (_match, alt: string, url: string) => {
      const apiPath = url.match(/\/api\/v1\/chat\/(?:files|images)\/[^)\s]+/);
      return `![${flattenAlt(alt)}](${apiPath?.[0] ?? url})`;
    },
  );
}

/** An alt text on one line, with no bracket that would end the image early. */
function flattenAlt(alt: string): string {
  return alt.replace(/[[\]]/g, "").replace(/\s+/g, " ").trim();
}

/**
 * The chat's image pass over an answer: API images flattened first, then
 * `![alt](file:<uuid>)` pointed at the file's download path, so the alt text
 * the answer wrote for a `file:` image is kept, then
 * `[text](file:<uuid>)` pointed at the same path.
 */
export function prepareChatImages(raw: string): string {
  return fileSchemeLinksToApi(fileSchemeImagesToApi(normalizeApiImageMarkdown(raw)));
}

/** The download path the chat shows an image file of the conversation from. */
export function chatFileDownloadPath(fileId: string): string {
  return `/api/v1/chat/files/${fileId.toLowerCase()}/download`;
}

/** `raw` with every `![alt](file:<uuid>)` pointing at the file's download path. */
export function fileSchemeImagesToApi(raw: string): string {
  if (!raw || !/file:/i.test(raw)) return raw;
  return raw.replace(
    FILE_SCHEME_IMAGE_RE,
    (_match, alt: string, fileId: string) => `![${alt}](${chatFileDownloadPath(fileId)})`,
  );
}

/** `raw` with every `[text](file:<uuid>)` link pointing at the file's download path. */
export function fileSchemeLinksToApi(raw: string): string {
  if (!raw || !/file:/i.test(raw)) return raw;
  return raw.replace(
    FILE_SCHEME_LINK_RE,
    (_match, text: string, fileId: string) => `[${text}](${chatFileDownloadPath(fileId)})`,
  );
}

/** The file id a link to a conversation file's download path names, else null. */
export function chatFileIdFromHref(href: string | undefined): string | null {
  return href?.match(CHAT_FILE_DOWNLOAD_RE)?.[1]?.toLowerCase() ?? null;
}
