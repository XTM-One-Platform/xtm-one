/**
 * Types for the node-native test discovery used by `vitest.config.ts`.
 *
 * The scanner itself stays plain `.mjs` so `node scripts/node-test-files.mjs`
 * runs it with no build step and no runner of its own — which is the point of
 * a script whose whole job is deciding which runner gets which file.
 */

/**
 * Every test file under `root` written against Node's test runner, as paths
 * relative to `root` with forward slashes.
 */
export function nodeTestFiles(root: string): string[];
