/**
 * Values a third party may put where the UI expects a web address, none of
 * which may ever reach an `href` (`lib/safe-url.ts`). One table, shared by the
 * helper's own test and by every surface that renders such a value, so a new
 * variant is covered everywhere at once.
 */
export const HOSTILE_URLS: ReadonlyArray<readonly [label: string, value: string]> = [
  ["javascript: scheme", "javascript:alert(1)"],
  ["javascript: in mixed case", "JaVaScRiPt:alert(1)"],
  ["javascript: after a leading space", " javascript:alert(1)"],
  ["javascript: split by a tab", "java\tscript:alert(1)"],
  ["data: scheme", "data:text/html,<script>alert(1)</script>"],
  ["vbscript: scheme", "vbscript:msgbox(1)"],
  ["file: scheme", "file:///etc/passwd"],
  ["root-relative path", "/admin/settings"],
  ["relative path", "admin/settings"],
  ["protocol-relative URL", "//evil.example/phish"],
  ["userinfo hiding the host", "https://trusted.example@evil.example/"],
];
