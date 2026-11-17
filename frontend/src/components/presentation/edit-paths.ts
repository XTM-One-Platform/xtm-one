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
 * Spec paths for the in-place slide editor.
 *
 * A path addresses one value inside a slide the way the spec nests it:
 * `blocks.2` is the third block of a content slide's run, `columns.0.1` the
 * second block of its first column, `blocks.2.children.1` a callout's second
 * child, `blocks.3.left.blocks.0` the first block of a comparison's left side,
 * `blocks.0.columns.1.0` the first block of the second column of a `columns`
 * BLOCK, `panels.1.block` a dashboard panel's figure, `footer_block` the slot
 * pinned at the bottom - and a text path continues into the block:
 * `blocks.2.items.0.text` is the first bullet, `title` the slide's own title.
 *
 * The renderer publishes these paths on the DOM (`data-edit-block`,
 * `data-edit-text`, see `edit-context.tsx`) and the editor writes through
 * them with the copy-on-write helpers below, so a click on the slide and a
 * write to the draft speak the same address - which is also the address the
 * agent's `edit_presentation` operations use (`presentation_patch.py`).
 *
 * Pure and dependency-free: `edit-paths.test.ts` runs it under Node's runner.
 */

type PathKey = string | number;
type EditPath = readonly PathKey[];

const DIGITS = /^\d+$/;

/** `"blocks.2.items.0.text"` -> `["blocks", 2, "items", 0, "text"]`. */
export function parsePath(path: string): EditPath {
  if (!path) return [];
  return path.split(".").map((segment) => (DIGITS.test(segment) ? Number(segment) : segment));
}

/** The inverse of `parsePath`. */
export function joinPath(path: EditPath): string {
  return path.map(String).join(".");
}

/** `path` extended by `more` segments (a block path + a field path). */
export function childPath(path: string, ...more: PathKey[]): string {
  const tail = more.map(String).join(".");
  if (!path) return tail;
  return tail ? `${path}.${tail}` : path;
}

/** The value at `path`, `undefined` when any step is missing. */
export function getAt(root: unknown, path: EditPath): unknown {
  let current: unknown = root;
  for (const key of path) {
    if (current === null || typeof current !== "object") return undefined;
    current = (current as Record<PathKey, unknown>)[key];
  }
  return current;
}

function copyContainer(container: unknown, key: PathKey): Record<PathKey, unknown> {
  if (Array.isArray(container)) return [...container] as unknown as Record<PathKey, unknown>;
  if (container !== null && typeof container === "object") return { ...(container as Record<PathKey, unknown>) };
  // A missing step is created as what the next key needs.
  return (typeof key === "number" ? [] : {}) as Record<PathKey, unknown>;
}

/**
 * `root` with `value` at `path` (copy-on-write along the path; every other
 * branch keeps its identity, so React keys and memos survive an edit). A
 * `value` of `undefined` DELETES the key - the spec's "absent" for an optional
 * field, which the validator prefers to `null` or `""`.
 */
export function setAt<T>(root: T, path: EditPath, value: unknown): T {
  if (path.length === 0) return value as T;
  const [key, ...rest] = path;
  const copy = copyContainer(root, key);
  if (rest.length === 0) {
    if (value === undefined) {
      if (Array.isArray(copy)) (copy as unknown[]).splice(key as number, 1);
      else delete copy[key];
    } else {
      copy[key] = value;
    }
  } else {
    copy[key] = setAt(copy[key], rest, value);
  }
  return copy as unknown as T;
}

/** `root` without the value at `path` (an array item is spliced out). */
export function removeAt<T>(root: T, path: EditPath): T {
  return setAt(root, path, undefined);
}

/** `root` with `value` inserted at `index` of the array at `parentPath`. */
export function insertAt<T>(root: T, parentPath: EditPath, index: number, value: unknown): T {
  const list = getAt(root, parentPath);
  const items = Array.isArray(list) ? [...list] : [];
  const at = Math.max(0, Math.min(index, items.length));
  items.splice(at, 0, value);
  return setAt(root, parentPath, items);
}

/** `root` with the item at `from` of the array at `parentPath` moved to `to`. */
export function moveAt<T>(root: T, parentPath: EditPath, from: number, to: number): T {
  const list = getAt(root, parentPath);
  if (!Array.isArray(list) || from === to || from < 0 || from >= list.length || to < 0 || to >= list.length) {
    return root;
  }
  const items = [...list];
  const [moved] = items.splice(from, 1);
  items.splice(to, 0, moved);
  return setAt(root, parentPath, items);
}

/**
 * Whether `path` lies inside `ancestor` (or is it): `blocks.2.children.1` is
 * inside `blocks.2`; `blocks.20` is not inside `blocks.2`.
 */
export function isWithin(path: string, ancestor: string): boolean {
  return path === ancestor || path.startsWith(`${ancestor}.`);
}

/**
 * The block path a text path lives in - the longest prefix that names a block
 * (`blocks.2.items.0.text` -> `blocks.2`; `footer_block.title` ->
 * `footer_block`; a slide-level `title` -> `null`). The block roots are the
 * places the spec nests blocks (`presentation_spec.py`): a run of `blocks`, a
 * column of `columns`, a callout's `children`, a comparison side's `blocks`, a
 * dashboard panel's `block`, the `footer_block` slot.
 */
export function blockPathOf(path: string): string | null {
  const parts = parsePath(path);
  let best: EditPath | null = null;
  for (let index = 0; index < parts.length; index += 1) {
    const key = parts[index];
    const next = parts[index + 1];
    if (key === "footer_block") best = parts.slice(0, index + 1);
    else if (key === "block" && index > 1 && parts[index - 2] === "panels" && typeof parts[index - 1] === "number") {
      best = parts.slice(0, index + 1);
    }
    else if ((key === "blocks" || key === "children") && typeof next === "number") best = parts.slice(0, index + 2);
    else if (key === "columns" && typeof next === "number" && typeof parts[index + 2] === "number") {
      best = parts.slice(0, index + 3);
    }
  }
  return best ? joinPath(best) : null;
}

/** One step of a block's position as a person reads it. */
export type BlockPlace =
  | { kind: "block" | "column" | "panel"; n: number }
  | { kind: "footer" | "left" | "right" };

/**
 * A block path as the steps a person reads (UX notes of #10269 r2/r3, A3:
 * the inspector showed the internal `blocks.0` / `columns.0.0`):
 * `columns.0.1` -> column 1, block 2; `blocks.3.left.blocks.0` -> block 4,
 * left side, block 1; `panels.2.block` -> panel 3; `footer_block` -> footer.
 * Numbers are 1-based. Keys that carry no position (`children`, `block`) add
 * nothing of their own.
 */
export function blockPlaces(path: string): BlockPlace[] {
  const parts = parsePath(path);
  const places: BlockPlace[] = [];
  for (let index = 0; index < parts.length; index += 1) {
    const key = parts[index];
    const next = parts[index + 1];
    if (key === "footer_block") places.push({ kind: "footer" });
    else if (key === "left" || key === "right") places.push({ kind: key });
    else if (key === "panels" && typeof next === "number") {
      places.push({ kind: "panel", n: next + 1 });
      index += 1;
    } else if (key === "columns" && typeof next === "number") {
      places.push({ kind: "column", n: next + 1 });
      index += 1;
      const block = parts[index + 1];
      if (typeof block === "number") {
        places.push({ kind: "block", n: block + 1 });
        index += 1;
      }
    } else if ((key === "blocks" || key === "children") && typeof next === "number") {
      places.push({ kind: "block", n: next + 1 });
      index += 1;
    }
  }
  return places;
}

/**
 * Where a block's SIBLINGS live: the array holding it and its index, or
 * `null` for a block that stands alone (`footer_block`, a panel's `block`).
 */
export function blockSlot(path: string): { list: EditPath; index: number } | null {
  const parts = parsePath(path);
  const last = parts[parts.length - 1];
  if (typeof last !== "number") return null;
  return { list: parts.slice(0, -1), index: last };
}
