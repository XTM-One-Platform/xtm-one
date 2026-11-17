/**
 * Unit tests for the local-path helpers of XTM One Desktop -
 * `yarn test:unit`.
 *
 * A reply is full of rooted strings that are not files (`/api/v1/chat`); a
 * false positive is a dead link, so the detector stays conservative. The shell
 * re-validates every request - these helpers only decide what is offered.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canOpenLocalPath,
  fileUrlToLocalPath,
  joinLocalPath,
  localPathExtension,
  localPathName,
  looksLikeLocalPath,
} from "./local-paths.ts";

test("windows paths are drive-rooted", () => {
  assert.equal(looksLikeLocalPath("C:\\Users\\me\\Desktop\\disks.txt", "windows"), true);
  assert.equal(looksLikeLocalPath("C:/Program Files/App/readme.md", "windows"), true);
  assert.equal(looksLikeLocalPath("~\\Documents", "windows"), true);
  for (const text of [
    "Desktop\\disks.txt",
    "C:disks.txt",
    "\\\\server\\share\\x",
    "//server/share",
    "C:\\a\\notes.txt:hidden",
    "C:\\a\\*.log",
    "C:\\a\nC:\\b",
    " C:\\padded",
    "/Users/me/x.txt",
  ]) {
    assert.equal(looksLikeLocalPath(text, "windows"), false, text);
  }
});

test("posix paths start at a real root, never at a route", () => {
  for (const text of [
    "/Users/me/report.pdf",
    "/home/me/.bashrc",
    "/tmp/out.log",
    "/var/log/syslog",
    "/Volumes/Backup/2026",
    "~/Downloads/data.csv",
  ]) {
    assert.equal(looksLikeLocalPath(text, "macos"), true, text);
    assert.equal(looksLikeLocalPath(text, "linux"), true, text);
  }
  for (const text of ["/api/v1/chat", "/presentations/123", "/chat", "relative/path", "~", "~/", "//host/x", "C:\\x"]) {
    assert.equal(looksLikeLocalPath(text, "linux"), false, text);
  }
});

test("file links become local paths, other hosts never do", () => {
  assert.equal(fileUrlToLocalPath("file:///C:/Users/me/My%20Report.pdf", "windows"), "C:\\Users\\me\\My Report.pdf");
  assert.equal(fileUrlToLocalPath("file:///home/me/a%20b.txt", "linux"), "/home/me/a b.txt");
  assert.equal(fileUrlToLocalPath("file://localhost/tmp/a.txt", "macos"), "/tmp/a.txt");
  assert.equal(fileUrlToLocalPath("file://attacker.example/share/x.pdf", "windows"), null);
  assert.equal(fileUrlToLocalPath("https://example.com/a.pdf", "linux"), null);
  assert.equal(fileUrlToLocalPath("file:///relative-on-windows", "windows"), null);
  assert.equal(fileUrlToLocalPath("file:///bad%E0%A4%A", "linux"), null);
});

test("a path the shell refuses is not offered: dot segments, shares, automount roots", () => {
  for (const text of ["C:\\Users\\..\\x.txt", "C:\\a\\.\\b.txt", "C:/a/../b.txt", "C:\\a\\...\\b.txt"]) {
    assert.equal(looksLikeLocalPath(text, "windows"), false, text);
  }
  for (const text of ["/tmp/../net/host/x", "/Users/me/./notes.md", "/home/../etc/passwd"]) {
    assert.equal(looksLikeLocalPath(text, "macos"), false, text);
  }
  assert.equal(looksLikeLocalPath("/Users/me/.config/app.toml", "macos"), true);
  assert.equal(looksLikeLocalPath("C:\\Users\\me\\.config\\app.toml", "windows"), true);
  // A file link is read as the path it names, with the same refusals.
  assert.equal(fileUrlToLocalPath("file:////server/share/x.pdf", "linux"), null);
  assert.equal(fileUrlToLocalPath("file:////server/share/x.pdf", "windows"), null);
  assert.equal(fileUrlToLocalPath("file:///net/attacker.example/x.pdf", "macos"), null);
  assert.equal(fileUrlToLocalPath("file:///NET/attacker.example/x.pdf", "macos"), null);
  assert.equal(fileUrlToLocalPath("file:///Network/Servers/x/y.pdf", "macos"), null);
  assert.equal(fileUrlToLocalPath("file:///srv/data/x.csv", "linux"), "/srv/data/x.csv");
});

test("names and extensions", () => {
  assert.equal(localPathName("C:\\Users\\me\\report.PDF"), "report.PDF");
  assert.equal(localPathName("/home/me/folder/"), "folder");
  assert.equal(localPathExtension("C:\\Users\\me\\report.PDF"), "pdf");
  assert.equal(localPathExtension("/home/me/.bashrc"), null);
  assert.equal(localPathExtension("/home/me/Makefile"), null);
  assert.equal(localPathExtension("/home/me/archive.tar.gz"), "gz");
});

test("a listing entry joins with the machine's separator", () => {
  assert.equal(joinLocalPath("C:\\Users\\me", "notes.txt", "windows"), "C:\\Users\\me\\notes.txt");
  assert.equal(joinLocalPath("C:\\", "Users", "windows"), "C:\\Users");
  assert.equal(joinLocalPath("/home/me", "notes.txt", "linux"), "/home/me/notes.txt");
  assert.equal(joinLocalPath("/", "tmp", "macos"), "/tmp");
  // An unknown platform still reads a drive-rooted path as Windows.
  assert.equal(joinLocalPath("D:\\data", "x.csv", null), "D:\\data\\x.csv");
});

test("Open is offered for what the shell announced it opens", () => {
  const extensions = ["txt", "pdf", "png"];
  assert.equal(canOpenLocalPath("C:\\a\\report.pdf", false, extensions), true);
  assert.equal(canOpenLocalPath("C:\\a\\setup.exe", false, extensions), false);
  assert.equal(canOpenLocalPath("/home/me/run", false, extensions), false);
  assert.equal(canOpenLocalPath("/home/me/Downloads", true, extensions), true);
  assert.equal(canOpenLocalPath("/Applications/Tool.app", true, extensions), false);
  assert.equal(canOpenLocalPath("C:\\a\\report.pdf", false, []), false);
});
