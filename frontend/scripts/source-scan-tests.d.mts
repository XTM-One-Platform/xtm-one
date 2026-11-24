/**
 * Types for the source-scan test discovery the "Frontend gates" job runs
 * (`scripts/source-scan-tests.mjs`).
 */

/** Whether a test file's source scans the tree: it imports `fs` and lists a directory. */
export function scansTheTree(source: string): boolean;

/** Every vitest file under `root` that scans the tree, relative to `root` with forward slashes. */
export function sourceScanTests(root: string): string[];
