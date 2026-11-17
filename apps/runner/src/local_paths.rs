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

//! Local paths the chat page may ask the shell to open or show.
//!
//! XTM One Desktop renders the paths an agent touched on THIS machine - the
//! file it read, wrote or listed, a path quoted in a reply - with `Open` and
//! `Show in folder` actions. The page asking is remote web content (the
//! enrolled instance's origin), so it never gets the opener plugin's path
//! commands: those act on any path a page names (a UNC path makes Windows
//! authenticate to whatever server it names; opening an executable runs it).
//! It gets two commands of the shell's own, and this module is their whole
//! validation:
//!
//! * the path must be an absolute LOCAL path (`~` expanded, `file://` read):
//!   network shares, device namespaces, NTFS alternate streams, reserved device
//!   names, `.` / `..` segments and the automount roots are refused before the
//!   file system is touched ([`parse_local_path`]);
//! * it must exist, and every decision is taken on the canonical target -
//!   links resolved, and the target shape-checked again, since a link on a
//!   local disk can lead onto a share ([`resolve_existing`]);
//! * `Open` only hands PASSIVE content to its default application - an
//!   allow-list of document / image / media / plain-text extensions checked on
//!   both the name asked for and the canonical target; a folder opens in the
//!   file manager unless it is a bundle (its name carries an extension - an
//!   `.app`, a `{CLSID}` junction - or it holds `Contents/Info.plist`)
//!   ([`plan_open`]); anything else is shown in its folder instead - `Show in
//!   folder` selects the item in the file manager and never executes anything.
//!
//! The shape checks are plain string functions for BOTH platform families, so
//! the Windows rules are tested on Linux and the other way round; the running
//! platform picks its own ([`check_local_shape`]).

use std::path::{Path, PathBuf};

/// Extensions `Open` hands to the default application: passive content only.
/// Never an executable, script, shortcut, installer, disk image, archive, web
/// page or document able to carry macros - their default action can run code
/// on at least one OS. Published to the page in the bridge facts, so it only
/// offers `Open` where the shell will honour it.
pub const OPENABLE_EXTENSIONS: &[&str] = &[
    // Plain text and data (no XML: a browser, the usual default viewer, runs
    // the script of an XHTML-namespaced file and of its XSLT stylesheet).
    "txt", "md", "markdown", "log", "csv", "tsv", "json", "yaml", "yml", "toml",
    // Documents that cannot carry macros (no OpenDocument: every ODF file may).
    "pdf", "docx", "xlsx", "pptx",
    // Images (no SVG: its default viewer is usually a browser running its script).
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic",
    // Audio and video.
    "mp3", "wav", "m4a", "flac", "ogg", "mp4", "mov", "webm", "mkv",
];

/// Longest path the page may name (characters).
pub const MAX_PATH_CHARS: usize = 4096;

/// Why a local-path request was refused. [`LocalPathError::code`] is the
/// stable token the page maps to its own wording.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LocalPathError {
    /// Not an absolute local path: empty, relative, too long, malformed.
    Invalid,
    /// A network share, a device namespace or an automount root.
    Remote,
    /// Nothing exists at that path on this machine.
    NotFound,
    /// It exists, but `Open` does not hand that kind of item to an application.
    NotOpenable,
}

impl LocalPathError {
    pub fn code(self) -> &'static str {
        match self {
            LocalPathError::Invalid => "invalid_path",
            LocalPathError::Remote => "remote_path",
            LocalPathError::NotFound => "not_found",
            LocalPathError::NotOpenable => "not_openable",
        }
    }
}

/// Device names Windows resolves in any directory, whatever the extension.
const WINDOWS_RESERVED_NAMES: &[&str] = &[
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "COM0", "COM1", "COM2", "COM3", "COM4",
    "COM5", "COM6", "COM7", "COM8", "COM9", "LPT0", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6",
    "LPT7", "LPT8", "LPT9",
];

/// The superscript spellings Windows resolves to the same devices (`COM`
/// and `LPT` followed by the superscript digit one, two or three).
const WINDOWS_RESERVED_SUPERSCRIPT_NAMES: &[&str] = &[
    "COM\u{b9}",
    "COM\u{b2}",
    "COM\u{b3}",
    "LPT\u{b9}",
    "LPT\u{b2}",
    "LPT\u{b3}",
];

/// First components where merely LOOKING UP a path makes the machine mount a
/// remote host's export (the `-hosts` automounter on macOS and Linux). Matched
/// whatever the case: macOS volumes are case-insensitive.
const AUTOMOUNT_ROOTS: &[&str] = &["net", "Network"];

/// Parse what the page sent into an absolute local path, touching nothing.
///
/// Accepts a plain path, `~` / `~/...` (expanded with `home`), or a `file:`
/// URL naming no host (or `localhost`). Everything else is refused here,
/// before any file-system call could reach a network location.
pub fn parse_local_path(raw: &str, home: Option<&Path>) -> Result<PathBuf, LocalPathError> {
    let raw = raw.trim();
    if raw.is_empty() || raw.chars().count() > MAX_PATH_CHARS || raw.chars().any(char::is_control) {
        return Err(LocalPathError::Invalid);
    }
    let path = if raw
        .get(..5)
        .is_some_and(|scheme| scheme.eq_ignore_ascii_case("file:"))
    {
        let url = url::Url::parse(raw).map_err(|_| LocalPathError::Invalid)?;
        match url.host_str() {
            None | Some("") => {}
            Some(host) if host.eq_ignore_ascii_case("localhost") => {}
            Some(_) => return Err(LocalPathError::Remote),
        }
        url.to_file_path().map_err(|_| LocalPathError::Invalid)?
    } else if raw == "~" || raw.starts_with("~/") || raw.starts_with("~\\") {
        let home = home.ok_or(LocalPathError::Invalid)?;
        let rest = raw[1..].trim_start_matches(['/', '\\']);
        if rest.is_empty() {
            home.to_path_buf()
        } else {
            home.join(rest)
        }
    } else {
        PathBuf::from(raw)
    };
    let text = path.to_str().ok_or(LocalPathError::Invalid)?;
    // Again after decoding: a `file:` URL turns `%00` / `%0A` into characters.
    if text.chars().any(char::is_control) {
        return Err(LocalPathError::Invalid);
    }
    check_local_shape(text)?;
    Ok(path)
}

/// The running platform's shape rules.
pub fn check_local_shape(text: &str) -> Result<(), LocalPathError> {
    if cfg!(windows) {
        windows_shape(text)
    } else {
        unix_shape(text)
    }
}

/// A Windows path the page may name: `X:\...` (or `X:/...`) on a drive letter.
fn windows_shape(text: &str) -> Result<(), LocalPathError> {
    // `\\server\share`, `//server/share`, `\\?\...`, `\\.\...`: a network
    // share or a device namespace.
    if text.starts_with("\\\\")
        || text.starts_with("//")
        || text.starts_with("\\/")
        || text.starts_with("/\\")
    {
        return Err(LocalPathError::Remote);
    }
    let bytes = text.as_bytes();
    let drive_rooted = bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && (bytes[2] == b'\\' || bytes[2] == b'/');
    if !drive_rooted {
        // Relative, drive-relative (`C:notes.txt`) or rooted on the current
        // drive (`\Windows`): none says where it is.
        return Err(LocalPathError::Invalid);
    }
    let rest = &text[2..];
    // A second colon names an NTFS alternate stream (`notes.txt:hidden`).
    if rest.contains(':') || rest.contains(['*', '?', '"', '<', '>', '|']) {
        return Err(LocalPathError::Invalid);
    }
    for component in rest.split(['\\', '/']).filter(|c| !c.is_empty()) {
        // `.`, `..` (and `...`, which Windows reads as `.`): the path is
        // judged as written, so it must say where it is without resolving.
        if component.chars().all(|c| c == '.') {
            return Err(LocalPathError::Invalid);
        }
        if is_reserved_device_name(component) {
            return Err(LocalPathError::Invalid);
        }
    }
    Ok(())
}

/// Whether a path component carries a Windows reserved device name (`CON`,
/// `NUL.txt`, `NUL:`, `COM1 `...), whatever its extension. The page-facing
/// check refuses them all: releases before Windows 11 open every such name as
/// a device. (File operations judge nothing by name: `capabilities::files`
/// lets what Windows opens decide.)
fn is_reserved_device_name(component: &str) -> bool {
    let stem = component
        .split(['.', ':'])
        .next()
        .unwrap_or("")
        .trim_end_matches(' ');
    WINDOWS_RESERVED_NAMES
        .iter()
        .chain(WINDOWS_RESERVED_SUPERSCRIPT_NAMES)
        .any(|reserved| reserved.eq_ignore_ascii_case(stem))
}

/// A macOS / Linux path the page may name: rooted, off the automount roots.
fn unix_shape(text: &str) -> Result<(), LocalPathError> {
    if !text.starts_with('/') || text.starts_with("//") {
        return Err(LocalPathError::Invalid);
    }
    // Refused rather than resolved: `/tmp/../net/host` only reaches `/net`
    // inside `canonicalize`, after the automounter contacted the host.
    if text.split('/').any(|c| c == "." || c == "..") {
        return Err(LocalPathError::Invalid);
    }
    let first = text[1..].split('/').next().unwrap_or("");
    if AUTOMOUNT_ROOTS
        .iter()
        .any(|root| root.eq_ignore_ascii_case(first))
    {
        return Err(LocalPathError::Remote);
    }
    Ok(())
}

/// An existing item, as the file system resolved it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedPath {
    /// The canonical target (links resolved), in the form the OS shell APIs
    /// take (no `\\?\` prefix on Windows).
    pub path: PathBuf,
    pub is_dir: bool,
    /// A folder holding `Contents/Info.plist`: macOS opens such a package as
    /// the application it is, extension or not.
    pub is_bundle: bool,
    /// A file with an execute bit, on Linux and the BSDs, where the file
    /// opener may pick its handler from what the file holds, not its name.
    pub is_executable: bool,
}

/// Resolve `path` (already shape-checked) to the item it names.
///
/// Blocking: call it off the async runtime. The canonical target is judged
/// like the name was ([`check_target`]): a link decides where a path leads,
/// whatever its own name says. (The lookup itself followed such a link -
/// planting one takes a local write, and on Windows the right to create
/// symbolic links - but nothing is opened or shown there.)
pub fn resolve_existing(path: &Path) -> Result<ResolvedPath, LocalPathError> {
    let canonical = std::fs::canonicalize(path).map_err(|_| LocalPathError::NotFound)?;
    let canonical = shell_form(canonical);
    let Some(text) = canonical.to_str() else {
        return Err(LocalPathError::Invalid);
    };
    check_target(text, on_mapped_network_drive(path))?;
    let meta = std::fs::metadata(&canonical).map_err(|_| LocalPathError::NotFound)?;
    let is_dir = meta.is_dir();
    let is_bundle = is_dir && canonical.join("Contents").join("Info.plist").is_file();
    let is_executable = !is_dir && runs_as_program(&meta);
    Ok(ResolvedPath {
        path: canonical,
        is_dir,
        is_bundle,
        is_executable,
    })
}

/// Where a resolved path LEADS must be local too: a target on a share or an
/// automount root is refused, except through a drive letter the person mapped
/// to a share themselves (`via_mapped_drive`) - there the share is theirs, and
/// Windows resolves every such path onto it.
fn check_target(canonical: &str, via_mapped_drive: bool) -> Result<(), LocalPathError> {
    match check_local_shape(canonical) {
        Err(LocalPathError::Remote) if via_mapped_drive => Ok(()),
        other => other,
    }
}

/// Whether `path` is named on a drive letter mapped to a network share.
#[cfg(windows)]
fn on_mapped_network_drive(path: &Path) -> bool {
    use windows_sys::Win32::Storage::FileSystem::GetDriveTypeW;
    use windows_sys::Win32::System::WindowsProgramming::DRIVE_REMOTE;

    let Some(letter) = path
        .to_str()
        .map(str::as_bytes)
        .filter(|bytes| bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':')
        .map(|bytes| bytes[0] as char)
    else {
        return false;
    };
    let root: Vec<u16> = format!("{letter}:\\")
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    // SAFETY: `root` is a NUL-terminated UTF-16 string that outlives the call.
    unsafe { GetDriveTypeW(root.as_ptr()) == DRIVE_REMOTE }
}

#[cfg(not(windows))]
fn on_mapped_network_drive(_path: &Path) -> bool {
    false
}

#[cfg(all(unix, not(target_os = "macos")))]
fn runs_as_program(meta: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    meta.permissions().mode() & 0o111 != 0
}

/// macOS picks the handler from the extension, Windows has no execute bit.
#[cfg(not(all(unix, not(target_os = "macos"))))]
fn runs_as_program(_meta: &std::fs::Metadata) -> bool {
    false
}

/// `std::fs::canonicalize` answers `\\?\C:\...` / `\\?\UNC\server\share\...`
/// on Windows; Explorer and `ShellExecute` want the ordinary forms, and so
/// does a comparison with a path as written (`capabilities::files`).
pub fn shell_form(path: PathBuf) -> PathBuf {
    if !cfg!(windows) {
        return path;
    }
    let Some(text) = path.to_str() else {
        return path;
    };
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        let bytes = rest.as_bytes();
        if bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' {
            return PathBuf::from(rest);
        }
    }
    path
}

/// The lower-cased extension of the last component, if any.
fn extension_of(path: &Path) -> Option<String> {
    path.extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| ext.to_ascii_lowercase())
}

fn openable(path: &Path) -> bool {
    extension_of(path)
        .map(|ext| OPENABLE_EXTENSIONS.contains(&ext.as_str()))
        .unwrap_or(false)
}

/// What `Open` hands to the OS: the canonical target, or a refusal (the
/// caller shows the item in its folder instead).
///
/// A file needs an allow-listed extension on BOTH the name asked for and the
/// canonical target (a `notes.txt` link onto an executable is refused), and
/// no execute bit where the opener may sniff what the file holds. A folder
/// opens in the file manager unless it is a bundle: either name carries an
/// extension (on macOS an `.app`, a `.prefPane`, a `.bundle`... folder IS a
/// program; on Windows a `{CLSID}` suffix turns a folder into a shell
/// namespace) or it holds `Contents/Info.plist`.
pub fn plan_open(input: &Path, resolved: &ResolvedPath) -> Result<PathBuf, LocalPathError> {
    if resolved.is_dir {
        if resolved.is_bundle
            || extension_of(input).is_some()
            || extension_of(&resolved.path).is_some()
        {
            return Err(LocalPathError::NotOpenable);
        }
        return Ok(resolved.path.clone());
    }
    if !resolved.is_executable && openable(input) && openable(&resolved.path) {
        Ok(resolved.path.clone())
    } else {
        Err(LocalPathError::NotOpenable)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_accepts_drive_rooted_paths_only() {
        assert_eq!(windows_shape(r"C:\Users\me\report.pdf"), Ok(()));
        assert_eq!(windows_shape("c:/Users/me/report.pdf"), Ok(()));
        assert_eq!(windows_shape(r"D:\"), Ok(()));
        for relative in [
            r"report.pdf",
            r"C:report.pdf",
            r"\Windows\win.ini",
            r".\a.txt",
            "",
        ] {
            assert_eq!(
                windows_shape(relative),
                Err(LocalPathError::Invalid),
                "{relative}"
            );
        }
    }

    #[test]
    fn windows_refuses_shares_and_device_namespaces_before_any_io() {
        for remote in [
            r"\\attacker.example\share\x.pdf",
            "//attacker.example/share/x.pdf",
            r"\\?\C:\Windows\notepad.exe",
            r"\\?\UNC\server\share\x",
            r"\\.\PhysicalDrive0",
            r"\/server/share",
        ] {
            assert_eq!(
                windows_shape(remote),
                Err(LocalPathError::Remote),
                "{remote}"
            );
        }
    }

    #[test]
    fn windows_refuses_streams_wildcards_and_device_names() {
        for bad in [
            r"C:\notes.txt:hidden",
            r"C:\notes.txt::$DATA",
            r"C:\a\*.txt",
            r"C:\a\CON",
            r"C:\a\nul.txt",
            r"C:\a\Com1 .log",
            r"C:\LPT9\x.txt",
            r"C:\a\conout$",
            r"C:\a\COM0.txt",
            r"C:\a\lpt0",
            "C:\\a\\COM\u{b9}.log",
            "C:\\a\\lpt\u{b3}",
        ] {
            assert_eq!(windows_shape(bad), Err(LocalPathError::Invalid), "{bad}");
        }
        // Names that merely start like a device are ordinary.
        assert_eq!(windows_shape(r"C:\a\console.txt"), Ok(()));
        assert_eq!(windows_shape(r"C:\a\company\nullable.md"), Ok(()));
    }

    #[test]
    fn unix_accepts_rooted_paths_off_the_automount_roots() {
        assert_eq!(unix_shape("/home/me/report.pdf"), Ok(()));
        assert_eq!(unix_shape("/Users/me/Desktop"), Ok(()));
        assert_eq!(unix_shape("/network-drive-backup/x"), Ok(()));
        assert_eq!(unix_shape("/netdata/x.log"), Ok(()));
        for bad in ["home/me", "./x", "", "//server/x"] {
            assert_eq!(unix_shape(bad), Err(LocalPathError::Invalid), "{bad}");
        }
        for remote in ["/net/attacker.example/export", "/net", "/Network/Servers/x"] {
            assert_eq!(unix_shape(remote), Err(LocalPathError::Remote), "{remote}");
        }
    }

    #[test]
    fn the_automount_roots_are_matched_whatever_their_case() {
        // macOS volumes are case-insensitive: `/NET/host` IS `/net/host` there.
        for remote in [
            "/NET/attacker.example/export",
            "/Net",
            "/network/servers/x",
            "/NETWORK/Servers/x",
        ] {
            assert_eq!(unix_shape(remote), Err(LocalPathError::Remote), "{remote}");
        }
    }

    #[test]
    fn dot_segments_are_refused_before_they_can_walk_onto_a_share() {
        // The shape check reads the path AS WRITTEN; a `..` is only resolved
        // inside `canonicalize` - after the automounter contacted the host.
        for bad in [
            "/tmp/../net/attacker.example/export",
            "/./net/attacker.example/x",
            "/../net/attacker.example/x",
            "/home/me/./notes.md",
        ] {
            assert_eq!(unix_shape(bad), Err(LocalPathError::Invalid), "{bad}");
        }
        for bad in [
            r"C:\Users\..\..\x.txt",
            r"C:\a\.\b.txt",
            r"C:\a\...\b.txt",
            "C:/a/../b.txt",
        ] {
            assert_eq!(windows_shape(bad), Err(LocalPathError::Invalid), "{bad}");
        }
        // A name that merely starts with a dot is ordinary.
        assert_eq!(unix_shape("/home/me/.config/app.toml"), Ok(()));
        assert_eq!(windows_shape(r"C:\Users\me\.config\app.toml"), Ok(()));
    }

    #[test]
    fn home_and_file_urls_are_read_into_paths() {
        let home = std::env::temp_dir();
        let expanded = parse_local_path("~/notes.md", Some(&home)).unwrap();
        assert_eq!(expanded, home.join("notes.md"));
        assert_eq!(parse_local_path("~", Some(&home)).unwrap(), home);
        assert_eq!(
            parse_local_path("~/notes.md", None),
            Err(LocalPathError::Invalid)
        );
        // A file URL naming a host is a network location, whatever follows.
        assert_eq!(
            parse_local_path("file://attacker.example/share/x.pdf", Some(&home)),
            Err(LocalPathError::Remote)
        );
        let local = if cfg!(windows) {
            "file:///C:/Users/me/My%20Report.pdf"
        } else {
            "file:///home/me/My%20Report.pdf"
        };
        let parsed = parse_local_path(local, Some(&home)).unwrap();
        assert!(parsed.to_str().unwrap().ends_with("My Report.pdf"));
        let localhost = if cfg!(windows) {
            "file://localhost/C:/a.txt"
        } else {
            "file://localhost/tmp/a.txt"
        };
        assert!(parse_local_path(localhost, Some(&home)).is_ok());
    }

    #[test]
    fn junk_is_refused_before_it_is_a_path() {
        assert_eq!(parse_local_path("   ", None), Err(LocalPathError::Invalid));
        assert_eq!(
            parse_local_path("/tmp/a\u{0}b", None),
            Err(LocalPathError::Invalid)
        );
        assert_eq!(
            parse_local_path("/tmp/a\nb", None),
            Err(LocalPathError::Invalid)
        );
        let long = format!("/{}", "a".repeat(MAX_PATH_CHARS + 1));
        assert_eq!(parse_local_path(&long, None), Err(LocalPathError::Invalid));
        assert_eq!(
            parse_local_path("https://example.com/a.pdf", None),
            Err(LocalPathError::Invalid)
        );
        assert_eq!(
            parse_local_path("javascript:alert(1)", None),
            Err(LocalPathError::Invalid)
        );
        // A multi-byte prefix is a path to check, never a slicing panic.
        assert_eq!(
            parse_local_path("\u{e9}\u{e9}\u{e9}", None),
            Err(LocalPathError::Invalid)
        );
    }

    fn file(p: &str) -> ResolvedPath {
        ResolvedPath {
            path: PathBuf::from(p),
            is_dir: false,
            is_bundle: false,
            is_executable: false,
        }
    }

    fn dir(p: &str) -> ResolvedPath {
        ResolvedPath {
            path: PathBuf::from(p),
            is_dir: true,
            is_bundle: false,
            is_executable: false,
        }
    }

    #[test]
    fn an_executable_file_is_never_opened_whatever_its_name() {
        // Linux file openers may pick the handler from the content: a script
        // named `notes.txt` with its execute bit set is only shown.
        let script = ResolvedPath {
            is_executable: true,
            ..file("/home/me/notes.txt")
        };
        assert_eq!(
            plan_open(Path::new("/home/me/notes.txt"), &script),
            Err(LocalPathError::NotOpenable)
        );
    }

    #[test]
    fn decoded_control_characters_are_refused_too() {
        let (nul, newline) = if cfg!(windows) {
            ("file:///C:/a%00b.txt", "file:///C:/a%0Ab.txt")
        } else {
            ("file:///tmp/a%00b.txt", "file:///tmp/a%0Ab.txt")
        };
        assert_eq!(parse_local_path(nul, None), Err(LocalPathError::Invalid));
        assert_eq!(
            parse_local_path(newline, None),
            Err(LocalPathError::Invalid)
        );
    }

    #[test]
    fn open_is_refused_for_active_content_whatever_its_case() {
        let names = never_opened().map(|ext| format!("x.{ext}")).chain([
            "Report.PDF.exe".into(),
            "SETUP.EXE".into(),
            "x.XML".into(),
        ]);
        for name in names {
            assert_eq!(
                plan_open(Path::new(&name), &file(&name)),
                Err(LocalPathError::NotOpenable),
                "{name}"
            );
        }
        for passive in ["a.pdf", "b.TXT", "c.png", "d.docx", "e.csv", "f.mp4"] {
            assert_eq!(
                plan_open(Path::new(passive), &file(passive)),
                Ok(PathBuf::from(passive)),
                "{passive}"
            );
        }
    }

    #[test]
    fn the_open_plan_checks_the_name_and_the_target() {
        assert_eq!(
            plan_open(Path::new("/x/report.pdf"), &file("/x/report.pdf")),
            Ok(PathBuf::from("/x/report.pdf"))
        );
        // A passive-looking link onto something else is refused.
        assert_eq!(
            plan_open(Path::new("/x/notes.txt"), &file("/x/tool.exe")),
            Err(LocalPathError::NotOpenable)
        );
        // No extension: a file is not handed to an application...
        assert_eq!(
            plan_open(Path::new("/x/run"), &file("/x/run")),
            Err(LocalPathError::NotOpenable)
        );
        // ...a plain folder opens in the file manager...
        assert_eq!(
            plan_open(Path::new("/x/Downloads"), &dir("/x/Downloads")),
            Ok(PathBuf::from("/x/Downloads"))
        );
        // ...a folder that is a program or a namespace is only shown.
        for bundle in [
            "/Applications/Tool.app",
            "/Library/PreferencePanes/Pane.prefPane",
            "/Library/Screen Savers/Saver.saver",
            "/Library/Extensions/Driver.kext",
            "/x/Thing.plugin",
            "/x/Thing.bundle",
            "/x/Thing.qlgenerator",
            "/x/Thing.appex",
            "/x/Thing.xpc",
            "/x/Thing.framework",
            "/x/Thing.action",
            "/x/Thing.workflow",
            "/x/Thing.mpkg",
            r"C:\x\cp.{21EC2020-3AEA-1069-A2DD-08002B30309D}",
        ] {
            assert_eq!(
                plan_open(Path::new(bundle), &dir(bundle)),
                Err(LocalPathError::NotOpenable),
                "{bundle}"
            );
        }
        // A package needs no extension to be one: its Info.plist says so.
        let package = ResolvedPath {
            is_bundle: true,
            ..dir("/Users/me/Downloads/Invoice")
        };
        assert_eq!(
            plan_open(Path::new("/Users/me/Downloads/Invoice"), &package),
            Err(LocalPathError::NotOpenable)
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_passive_name_linked_onto_a_script_is_judged_by_its_target() {
        let dir = std::env::temp_dir().join(format!("xtm-local-links-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("tool.sh");
        std::fs::write(&script, b"#!/bin/sh\n").unwrap();
        let link = dir.join("notes.txt");
        let _ = std::fs::remove_file(&link);
        std::os::unix::fs::symlink(&script, &link).unwrap();
        let resolved = resolve_existing(&link).unwrap();
        assert_eq!(resolved.path.file_name().unwrap(), "tool.sh");
        assert_eq!(
            plan_open(&link, &resolved),
            Err(LocalPathError::NotOpenable)
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn resolving_needs_an_existing_item_and_reports_its_kind() {
        let dir = std::env::temp_dir().join(format!("xtm-local-paths-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("report.txt");
        std::fs::write(&file, b"hello").unwrap();
        let resolved = resolve_existing(&file).unwrap();
        assert!(!resolved.is_dir);
        assert!(!resolved.path.to_str().unwrap().starts_with(r"\\?\"));
        assert_eq!(resolved.path.file_name().unwrap(), "report.txt");
        let folder = resolve_existing(&dir).unwrap();
        assert!(folder.is_dir && !folder.is_bundle);
        assert_eq!(
            resolve_existing(&dir.join("missing.txt")),
            Err(LocalPathError::NotFound)
        );
        // A folder holding Contents/Info.plist is a package, extension or not.
        let package = dir.join("Invoice");
        std::fs::create_dir_all(package.join("Contents")).unwrap();
        std::fs::write(package.join("Contents").join("Info.plist"), b"<plist/>").unwrap();
        let resolved = resolve_existing(&package).unwrap();
        assert!(resolved.is_dir && resolved.is_bundle);
        assert_eq!(
            plan_open(&package, &resolved),
            Err(LocalPathError::NotOpenable)
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_link_is_judged_by_where_it_leads() {
        if cfg!(windows) {
            assert_eq!(check_target(r"C:\Users\me\notes.txt", false), Ok(()));
            // A link on a local disk onto a share, or a device path.
            assert_eq!(
                check_target(r"\\evil\share\notes.txt", false),
                Err(LocalPathError::Remote)
            );
            assert_eq!(
                check_target(r"\\?\Volume{0}\notes.txt", false),
                Err(LocalPathError::Remote)
            );
            assert_eq!(
                check_target(r"\\?\GLOBALROOT\Device\Mup\evil\share\notes.txt", false),
                Err(LocalPathError::Remote)
            );
            // A drive the person mapped resolves onto its share.
            assert_eq!(check_target(r"\\files\home\notes.txt", true), Ok(()));
            assert!(!on_mapped_network_drive(Path::new(r"C:\Windows")));
            assert!(!on_mapped_network_drive(Path::new(r"\\files\home")));
        } else {
            assert_eq!(check_target("/home/me/notes.txt", false), Ok(()));
            assert_eq!(
                check_target("/net/evil/notes.txt", false),
                Err(LocalPathError::Remote)
            );
            assert_eq!(
                check_target("/NETWORK/Servers/evil/notes.txt", false),
                Err(LocalPathError::Remote)
            );
            assert!(!on_mapped_network_drive(Path::new("/Volumes/files")));
        }
    }

    #[test]
    fn the_windows_shell_form_drops_the_verbatim_prefix() {
        if cfg!(windows) {
            assert_eq!(
                shell_form(PathBuf::from(r"\\?\C:\Users\me\a.txt")),
                PathBuf::from(r"C:\Users\me\a.txt")
            );
            assert_eq!(
                shell_form(PathBuf::from(r"\\?\UNC\files\home\a.txt")),
                PathBuf::from(r"\\files\home\a.txt")
            );
        } else {
            assert_eq!(shell_form(PathBuf::from("/tmp/a")), PathBuf::from("/tmp/a"));
        }
    }

    #[test]
    fn every_code_is_distinct_and_stable() {
        let codes = [
            LocalPathError::Invalid.code(),
            LocalPathError::Remote.code(),
            LocalPathError::NotFound.code(),
            LocalPathError::NotOpenable.code(),
        ];
        let unique: std::collections::HashSet<_> = codes.iter().collect();
        assert_eq!(unique.len(), codes.len());
        assert_eq!(
            codes,
            ["invalid_path", "remote_path", "not_found", "not_openable"]
        );
    }

    /// Extensions whose default handler, on at least one of the three OSes,
    /// runs code, installs, mounts, renders script or reaches the network -
    /// one group per line.
    const NEVER_OPENED: &[&str] = &[
        // Windows executables, scripts and script hosts.
        "exe com pif scr cpl msc bat cmd ps1 psm1 vbs vbe js jse wsf wsh wsc sct hta py pyw pyc pyz chm reg inf",
        // Windows shortcuts, shell and search namespaces, ClickOnce, themes.
        "lnk url library-ms search-ms searchconnector-ms settingcontent-ms appref-ms application xbap website theme themepack diagcab gadget",
        // Installers, packages, disk images (mounted on open), Java.
        "msi msp msix msixbundle appx appxbundle iso img vhd vhdx jar jnlp pkg mpkg dmg deb rpm appimage run bin sh",
        // Script-capable markup: the default viewer is usually a browser.
        "html htm xhtml mht mhtml svg xml xsl xslt",
        // Macro-capable documents: the Office `m` variants and legacy binaries,
        // OpenDocument (any ODF file may carry macros), RTF, OneNote, add-ins.
        "docm dotm xlsm xltm xlam xla xll pptm potm ppam ppsm ppsx sldm doc xls ppt rtf odt ods odp odg one iqy slk",
        // macOS launchers, locations, scripts and bundle types.
        "command tool terminal webloc inetloc fileloc workflow action scpt scptd applescript app prefpane saver plugin bundle kext qlgenerator appex xpc framework",
        // Linux launchers, and archives (an archive manager may extract on open).
        "desktop zip 7z rar tar gz",
    ];

    fn never_opened() -> impl Iterator<Item = &'static str> {
        NEVER_OPENED
            .iter()
            .flat_map(|group| group.split_whitespace())
    }

    #[test]
    fn the_allow_list_is_lower_case_and_passive() {
        for ext in OPENABLE_EXTENSIONS {
            assert_eq!(*ext, ext.to_ascii_lowercase());
            assert!(!ext.starts_with('.'));
        }
        assert!(never_opened().count() >= 100);
        for active in never_opened() {
            assert!(!OPENABLE_EXTENSIONS.contains(&active), "{active}");
        }
    }
}
