// SPDX-License-Identifier: Apache-2.0

/**
 * Builds a printable PDF of two-factor recovery codes, with no
 * dependency.
 *
 * Why client-side: the server cannot produce this document. It stores only
 * SHA-256 hashes of the codes and returns the plaintext exactly once, in the
 * response to `enable` / `recovery-codes`. By the time any "download" request
 * could reach the backend, the plaintext is gone for good. So the PDF is built
 * in the browser from the codes already on screen — it adds no new exposure,
 * because nothing crosses the network that had not already crossed it.
 *
 * Why no library: a jsPDF-class dependency is ~300 kB to lay out eleven lines
 * of text. The same reasoning that keeps QR rendering on the server (see
 * `services/mfa.qr_data_uri`) applies here — this file is the whole cost, and
 * it has no transitive surface.
 *
 * The output is a single A4 page, PDF 1.4, using two of the base-14 fonts
 * (Helvetica for prose, Courier for the codes) so nothing has to be embedded.
 */

/**
 * Map a JS string to a WinAnsi byte string (one char per byte).
 *
 * Base-14 fonts are single-byte: they can render Latin text but have no way to
 * express CJK without embedding a font file, which is exactly the payload
 * weight this module avoids. Returns `null` when the text cannot be represented
 * so the caller can fall back to English rather than emit a page of garbage
 * glyphs — a recovery-code sheet that looks corrupted is worse than one in the
 * wrong language.
 */
function toWinAnsi(text: string): string | null {
  // The handful of typographic characters that live outside Latin-1 but inside
  // WinAnsi. Anything else outside U+0020..U+00FF means "not representable".
  const SPECIAL: Record<string, number> = {
    "‘": 0x91, "’": 0x92, "“": 0x93, "”": 0x94,
    "–": 0x96, "—": 0x97, "•": 0x95, "€": 0x80,
    "…": 0x85,
  };
  let out = "";
  for (const char of text) {
    const code = char.codePointAt(0)!;
    if (code >= 0x20 && code <= 0xff) {
      out += String.fromCharCode(code);
    } else if (SPECIAL[char] !== undefined) {
      out += String.fromCharCode(SPECIAL[char]);
    } else {
      return null;
    }
  }
  return out;
}

/** Escape the three characters that are structural inside a PDF string. */
function escapePdfText(winAnsi: string): string {
  return winAnsi.replace(/[\\()]/g, (c) => `\\${c}`);
}

type Line = { text: string; size: number; font: "F1" | "F2"; gap: number };

/**
 * Labels for the document. Each carries its English original so a locale that
 * cannot be encoded degrades per-line to English instead of dropping out.
 */
type PdfStrings = {
  title: string;
  account: string;
  generated: string;
  warning: string;
  footer: string;
};

const ENGLISH_FALLBACK: PdfStrings = {
  title: "Two-factor recovery codes",
  account: "Account",
  generated: "Generated",
  warning:
    "Each code signs you in once if you lose your authenticator device. Keep this sheet somewhere safe.",
  footer: "These codes cannot be displayed again. Generate a new set to replace them.",
};

/** Encode a label, falling back to its English original when unrepresentable. */
function label(value: string, fallback: string): string {
  return toWinAnsi(value) ?? toWinAnsi(fallback) ?? fallback;
}

/**
 * Wrap an already-WinAnsi string to a character budget. Helvetica is
 * proportional so this is approximate, but the budget is set well inside the
 * page width and the only wrapped line is the warning sentence.
 */
function wrap(text: string, maxChars: number): string[] {
  const words = text.split(" ");
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (current && (current + " " + word).length > maxChars) {
      lines.push(current);
      current = word;
    } else {
      current = current ? `${current} ${word}` : word;
    }
  }
  if (current) lines.push(current);
  return lines;
}

/**
 * Render the document and return its bytes.
 *
 * @param codes     the plaintext recovery codes, in display order
 * @param email     the account they belong to
 * @param strings   localized labels; each falls back to English if unencodable
 * @param platform  product name for the title line
 * @param now       generation timestamp (injected so the output is testable)
 */
function buildRecoveryCodesPdf(
  codes: string[],
  email: string,
  strings: Partial<PdfStrings>,
  platform: string,
  now: Date,
): Uint8Array<ArrayBuffer> {
  const s: PdfStrings = {
    title: label(strings.title ?? ENGLISH_FALLBACK.title, ENGLISH_FALLBACK.title),
    account: label(strings.account ?? ENGLISH_FALLBACK.account, ENGLISH_FALLBACK.account),
    generated: label(strings.generated ?? ENGLISH_FALLBACK.generated, ENGLISH_FALLBACK.generated),
    warning: label(strings.warning ?? ENGLISH_FALLBACK.warning, ENGLISH_FALLBACK.warning),
    footer: label(strings.footer ?? ENGLISH_FALLBACK.footer, ENGLISH_FALLBACK.footer),
  };
  const platformLine = toWinAnsi(platform) ?? "XTM One";
  // ISO date: unambiguous on a printed sheet in any locale, unlike a localized
  // short date where 03/04 could be March or April.
  const stamp = now.toISOString().slice(0, 16).replace("T", " ") + " UTC";

  const lines: Line[] = [
    { text: platformLine, size: 10, font: "F1", gap: 26 },
    { text: s.title, size: 20, font: "F1", gap: 30 },
    { text: `${s.account}: ${toWinAnsi(email) ?? email}`, size: 11, font: "F1", gap: 16 },
    { text: `${s.generated}: ${stamp}`, size: 11, font: "F1", gap: 26 },
    ...wrap(s.warning, 78).map((text) => ({ text, size: 11, font: "F1" as const, gap: 16 })),
    { text: "", size: 11, font: "F1", gap: 14 },
    // Courier so the codes line up in a column and O/0 stay distinguishable.
    ...codes.map((code) => ({ text: code, size: 14, font: "F2" as const, gap: 22 })),
    { text: "", size: 11, font: "F1", gap: 12 },
    ...wrap(s.footer, 78).map((text) => ({ text, size: 9, font: "F1" as const, gap: 13 })),
  ];

  let cursorY = 780; // A4 is 842pt tall; start below the top margin.
  let content = "";
  for (const line of lines) {
    if (line.text) {
      content += `BT /${line.font} ${line.size} Tf 1 0 0 1 60 ${cursorY} Tm (${escapePdfText(line.text)}) Tj ET\n`;
    }
    cursorY -= line.gap;
  }

  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] " +
      "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${content.length} >>\nstream\n${content}endstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
  ];

  // Assemble, tracking byte offsets for the xref table. Every string here is a
  // byte string (one char = one byte), so `.length` IS the byte offset — which
  // is why the text was mapped to WinAnsi up front rather than at write time.
  let pdf = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((body, i) => {
    offsets.push(pdf.length);
    pdf += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });

  const xrefStart = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) {
    pdf += `${String(offset).padStart(10, "0")} 00000 n \n`;
  }
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefStart}\n%%EOF\n`;

  return Uint8Array.from(pdf, (c) => c.charCodeAt(0));
}

/** Build the PDF and hand it to the browser as a download. */
export function downloadRecoveryCodesPdf(
  codes: string[],
  email: string,
  strings: Partial<PdfStrings>,
  platform: string,
): void {
  const bytes = buildRecoveryCodesPdf(codes, email, strings, platform, new Date());
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
  const link = document.createElement("a");
  link.href = url;
  const slug = platform.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  link.download = `${slug || "xtm-one"}-recovery-codes.pdf`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoke on the next tick: revoking synchronously can cancel the download in
  // Safari, which reads the blob after the click handler returns.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
