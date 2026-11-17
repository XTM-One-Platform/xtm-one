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
 * Local paths in the chat - what XTM One Desktop can open or show.
 *
 * Inside the Desktop app, a path the agent touched on the machine the app runs
 * on (the file it read or wrote, the folder it listed, a path quoted in its
 * reply) gets `Open` / `Show in folder` actions, executed by the native shell
 * (`desktop_open_local_path` / `desktop_reveal_local_path`), which re-validates
 * everything: the page only decides what is worth offering.
 *
 * Pure helpers only (`node --test`).
 */
import type { RunnerPlatform } from "@/types";

/** Longest path offered as a local link. */
const MAX_LOCAL_PATH_CHARS = 1024;

/**
 * The first components of the macOS / Linux paths worth linking. A reply is
 * full of rooted strings that are NOT files on this machine (`/api/v1/chat`,
 * `/presentations/<id>`), so a POSIX path must start at a real root of the
 * file system people keep files under.
 */
const POSIX_ROOTS = new Set([
  "Users",
  "home",
  "tmp",
  "private",
  "var",
  "etc",
  "opt",
  "usr",
  "Volumes",
  "mnt",
  "media",
  "srv",
  "root",
  "Applications",
  "Library",
  "System",
  "snap",
]);

/**
 * First components where the automounter reads the rest as a remote host's
 * export - the shell refuses them whatever their case (`local_paths.rs`).
 */
const AUTOMOUNT_ROOTS = new Set(["net", "network"]);

const WINDOWS_DRIVE_RE = /^[A-Za-z]:[\\/]/;
const WINDOWS_FORBIDDEN_RE = /[<>"|?*\n\r\t]/;

/**
 * Whether `path` holds a `.` / `..` segment (on Windows, any all-dots one):
 * the shell judges a path as written and refuses them.
 */
function hasDotSegment(path: string, platform: RunnerPlatform): boolean {
  if (platform === "windows") return path.split(/[\\/]/).some((segment) => /^\.+$/.test(segment));
  return path.split("/").some((segment) => segment === "." || segment === "..");
}

/**
 * Whether `text` reads as an absolute path on a machine of `platform`:
 * `C:\Users\me\report.pdf` on Windows, `/Users/me/report.pdf` or `~/notes.md`
 * elsewhere. Deliberately conservative - a false positive is a dead link.
 */
export function looksLikeLocalPath(text: string, platform: RunnerPlatform): boolean {
  const value = text.trim();
  if (!value || value.length > MAX_LOCAL_PATH_CHARS || value !== text) return false;
  if (value.startsWith("\\\\") || value.startsWith("//")) return false;
  if (hasDotSegment(value, platform)) return false;
  if (platform === "windows") {
    if (value.startsWith("~\\") || value.startsWith("~/")) return value.length > 2;
    return WINDOWS_DRIVE_RE.test(value) && !WINDOWS_FORBIDDEN_RE.test(value) && !value.slice(2).includes(":");
  }
  if (/[\n\r\t]/.test(value)) return false;
  if (value === "~" || value.startsWith("~/")) return value.length > 2;
  if (!value.startsWith("/")) return false;
  const first = value.slice(1).split("/")[0];
  return POSIX_ROOTS.has(first);
}

/**
 * The local path a `file:` link points at, or `null` for what the shell would
 * refuse: another host, a share reached through an empty host
 * (`file:////server/share`), an automount root.
 */
export function fileUrlToLocalPath(href: string, platform: RunnerPlatform): string | null {
  if (!/^file:/i.test(href)) return null;
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.host && url.hostname.toLowerCase() !== "localhost") return null;
  let path: string;
  try {
    path = decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
  if (hasDotSegment(path, platform)) return null;
  if (platform === "windows") {
    // file:///C:/Users/me -> C:\Users\me
    const trimmed = path.replace(/^\/([A-Za-z]:)/, "$1");
    return WINDOWS_DRIVE_RE.test(trimmed) ? trimmed.replace(/\//g, "\\") : null;
  }
  if (!path.startsWith("/") || path.startsWith("//")) return null;
  return AUTOMOUNT_ROOTS.has(path.slice(1).split("/")[0].toLowerCase()) ? null : path;
}

/** The last component of a path (`report.pdf`), without trailing separators. */
export function localPathName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return cut === -1 ? trimmed : trimmed.slice(cut + 1);
}

/** The lower-cased extension of the last component, or `null`. */
export function localPathExtension(path: string): string | null {
  const name = localPathName(path);
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return null;
  return name.slice(dot + 1).toLowerCase();
}

/** `dir` + `name` with the machine's separator, as a listing's entry path. */
export function joinLocalPath(dir: string, name: string, platform: RunnerPlatform | null): string {
  const separator = platform === "windows" || /^[A-Za-z]:\\/.test(dir) ? "\\" : "/";
  return dir.endsWith("/") || dir.endsWith("\\") ? `${dir}${name}` : `${dir}${separator}${name}`;
}

/**
 * Whether `Open` is worth offering: a folder without an extension, or a file
 * whose extension the shell announced it opens (`open_extensions`). The shell
 * decides on the real target anyway.
 */
export function canOpenLocalPath(path: string, isDir: boolean, openExtensions: readonly string[]): boolean {
  const extension = localPathExtension(path);
  if (isDir) return extension === null;
  return extension !== null && openExtensions.includes(extension);
}
