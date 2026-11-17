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
 * The printable emergency procedure — the page an operator keeps on paper (or
 * as a PDF through the browser's print dialog) so the stop can be pressed
 * from any terminal when nobody is signed in.
 *
 * Built as a standalone HTML document and opened in a new window: the app's
 * own stylesheet is not printable, and the token must never end up in a URL.
 * Everything is escaped — the token and the names come from the API.
 */

interface EmergencyProcedureInput {
  platformName: string;
  origin: string;
  token: string;
  createdAt: string;
  createdBy: string;
  labels: {
    title: string;
    subtitle: string;
    whenTitle: string;
    whenBody: string;
    whatTitle: string;
    whatBody: string;
    howTitle: string;
    howBody: string;
    tokenTitle: string;
    tokenBody: string;
    afterTitle: string;
    afterBody: string;
    contactsTitle: string;
    contactsBody: string;
    generated: string;
    keepSafe: string;
  };
}

function esc(value: string): string {
  return value.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c);
}

export function emergencyStopCurl(origin: string, token: string): string {
  return [
    `curl -X POST ${origin}/api/v1/panic/emergency \\`,
    `  -H "Authorization: Bearer ${token}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"reason": "<why you are stopping everything>"}'`,
  ].join("\n");
}

function buildEmergencyProcedureHtml(input: EmergencyProcedureInput): string {
  const { labels: l } = input;
  const curl = emergencyStopCurl(input.origin, input.token);
  return `<!doctype html>
<html><head><meta charset="utf-8"><title>${esc(l.title)} — ${esc(input.platformName)}</title>
<style>
  @page { margin: 18mm; }
  body { font: 12pt/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #111; max-width: 180mm; margin: 0 auto; padding: 12mm; }
  h1 { font-size: 22pt; margin: 0 0 4pt; display: flex; align-items: center; gap: 10pt; }
  h1 .stop { display: inline-block; width: 22pt; height: 22pt; background: #c62828; clip-path: polygon(30% 0, 70% 0, 100% 30%, 100% 70%, 70% 100%, 30% 100%, 0 70%, 0 30%); }
  .sub { color: #555; margin: 0 0 16pt; }
  h2 { font-size: 13pt; margin: 16pt 0 4pt; border-bottom: 1px solid #ccc; padding-bottom: 2pt; }
  p { margin: 4pt 0; }
  pre { background: #f4f4f4; border: 1px solid #ddd; padding: 10pt; font: 10.5pt/1.4 "SFMono-Regular", Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-all; }
  .token { font: 14pt/1.4 "SFMono-Regular", Menlo, Consolas, monospace; letter-spacing: 0.5pt; border: 2px solid #c62828; padding: 8pt 10pt; display: inline-block; word-break: break-all; }
  .warn { background: #fff4e5; border-left: 4pt solid #ef6c00; padding: 6pt 10pt; }
  .lines p { border-bottom: 1px dotted #999; height: 18pt; margin: 6pt 0; }
  footer { margin-top: 20pt; font-size: 9.5pt; color: #666; }
  @media print { body { padding: 0; } }
</style></head><body>
<h1><span class="stop"></span>${esc(l.title)}</h1>
<p class="sub">${esc(input.platformName)} — ${esc(input.origin)} — ${esc(l.subtitle)}</p>
<h2>${esc(l.whenTitle)}</h2><p>${esc(l.whenBody)}</p>
<h2>${esc(l.whatTitle)}</h2><p>${esc(l.whatBody)}</p>
<h2>${esc(l.howTitle)}</h2><p>${esc(l.howBody)}</p>
<pre>${esc(curl)}</pre>
<h2>${esc(l.tokenTitle)}</h2>
<p><span class="token">${esc(input.token)}</span></p>
<p class="warn">${esc(l.tokenBody)}</p>
<h2>${esc(l.afterTitle)}</h2><p>${esc(l.afterBody)}</p>
<h2>${esc(l.contactsTitle)}</h2><p>${esc(l.contactsBody)}</p>
<div class="lines"><p></p><p></p><p></p></div>
<footer>${esc(l.generated)} ${esc(input.createdAt)} — ${esc(input.createdBy)}. ${esc(l.keepSafe)}</footer>
</body></html>`;
}

/** Opens the procedure in a new window and triggers the print dialog. */
export function printEmergencyProcedure(input: EmergencyProcedureInput): boolean {
  const win = window.open("", "_blank", "noopener=no,width=900,height=1100");
  if (!win) return false;
  win.document.open();
  win.document.write(buildEmergencyProcedureHtml(input));
  win.document.close();
  win.focus();
  // Let the document lay out before the print dialog captures it.
  win.setTimeout(() => win.print(), 300);
  return true;
}
