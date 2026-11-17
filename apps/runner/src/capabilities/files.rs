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

//! File operations: read_file, write_file, list_dir.
//!
//! Read returns small UTF-8 text inline (`{content}`); binary or oversized
//! content uploads as a file part so the backend persists it as a downloadable
//! `FileAttachment`. Either way it returns at most `max_bytes` and says whether
//! the file went on past them (`truncated`). Synchronous (runs inside a
//! blocking task).
//!
//! Confinement. A restricted runner with an allowed-paths list gets
//! those folders from the backend as `allowed_roots`. The backend checks the
//! path AS WRITTEN; only this side can see where a link leads, so the path and
//! the roots are resolved on the real file system (links followed) and where
//! the path lands is compared with the roots. A folder that does not exist yet
//! resolves through its nearest existing ancestor, so a write can still create
//! it, and a path written as a share or automount path is only looked up
//! inside a root written the same way. A path that leads outside the roots
//! through a link runs as it always has, with a warning in its result (and in
//! the log, the first time), unless the backend sends `strict_confinement` -
//! the operator's `RUNNER_STRICT_PATH_CONFINEMENT` opt-in - which refuses it.
//! The operation runs on the path as given, as it does without roots, where
//! full file access is what the capability grants.
//! A path goes to the OS as written and is never judged by its name: what the
//! OS opens for it decides. A device, a FIFO or a socket - its type as the OS
//! reports it, from the path's stat and from the opened handle (on Windows the
//! handle's file type, since Windows cannot describe a device handle) - is
//! read and written as it always was, with a warning in the result and the
//! log, and `strict_confinement` refuses it, whatever the runner's mode. A
//! Windows name is a device only when the handle Windows opens is one, and a
//! file on a volume or a shadow copy is a file. When the OS refuses a path, its
//! error is the result. Such a file is opened non-blocking and then set back to
//! blocking, so a FIFO with no other end returns instead of holding a runner
//! worker for good.
//!
//! Confined commands re-check the object they actually OPENED, not just the
//! path checked before it: its type from the handle (an fstat, which a device
//! swap cannot fool) and its real path, held to the roots like the path was,
//! so in strict mode a link swapped in between the check and the open is
//! caught too. Nothing is truncated or written until the handle is verified;
//! only a folder a write has to create is made before, so in strict mode a link
//! swapped in at that moment can still leave an empty folder where it leads.
//!
//! What no check here can see: a local link onto a share is followed to find
//! where it leads (which can reach that host), and a hard link is the file
//! itself under a second name.

use std::fs::{File, OpenOptions};
use std::path::{Component, Path, PathBuf};

use anyhow::{anyhow, Context, Result};

use crate::local_paths::{self, LocalPathError};
use crate::protocol::{CommandOutcome, FilePart};

/// Text at or below this size is returned inline; larger reads upload as a file.
const INLINE_TEXT_LIMIT: usize = 262_144; // 256 KiB
/// Default read ceiling when the agent gives no `max_bytes`.
const DEFAULT_READ_LIMIT: u64 = 16_777_216; // 16 MiB
/// Links followed while resolving one path before it counts as a loop.
const MAX_LINK_HOPS: usize = 40;
/// Distinct confinement warnings the log remembers before it starts over.
const LOGGED_WARNINGS: usize = 1024;

/// What the backend confines a restricted runner's file command to.
struct Confinement {
    /// The allowed folders ([`allowed_roots`]).
    roots: Vec<PathBuf>,
    /// Refuse a path that leads outside `roots`; otherwise it runs, as it
    /// always did, with a warning.
    strict: bool,
}

/// The confinement a command carries; `None`: none (full control, no
/// allowed-paths list, or a backend that sends none).
fn confinement(payload: &serde_json::Value) -> Option<Confinement> {
    Some(Confinement {
        roots: allowed_roots(payload)?,
        strict: strict_requested(payload),
    })
}

/// The operator's `RUNNER_STRICT_PATH_CONFINEMENT` opt-in, which the backend
/// sends with every file command while it is on, full control included.
fn strict_requested(payload: &serde_json::Value) -> bool {
    payload
        .get("strict_confinement")
        .and_then(|strict| strict.as_bool())
        .unwrap_or(false)
}

/// The folders a restricted runner may touch, as the backend sent them
/// ([`absolute`]), local ones first so a share is only looked up when no local
/// folder holds the path. `None` (absent or `null`): no confinement; any other
/// value that is not a list confines to no folder at all.
fn allowed_roots(payload: &serde_json::Value) -> Option<Vec<PathBuf>> {
    let listed = payload
        .get("allowed_roots")
        .filter(|roots| !roots.is_null())?;
    let mut roots: Vec<PathBuf> = listed
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|root| root.as_str())
        .filter(|root| !root.trim().is_empty())
        .filter_map(|root| absolute(&whole_drive(root)))
        .collect();
    roots.sort_by_key(|root| is_remote(root));
    Some(roots)
}

/// An allowed folder written as a bare drive (`D:`) is that whole drive, as
/// the backend's check reads it; Windows alone would take it for the drive's
/// current folder.
fn whole_drive(root: &str) -> PathBuf {
    match root.as_bytes() {
        [letter, b':'] if cfg!(windows) && letter.is_ascii_alphabetic() => {
            PathBuf::from(format!("{root}\\"))
        }
        _ => PathBuf::from(root),
    }
}

/// A device, a FIFO or a socket rather than a regular file. The operation runs
/// on it as it always did, with a warning in its result (and in the log, the
/// first time); `strict` (`RUNNER_STRICT_PATH_CONFINEMENT`) refuses it.
fn special_file(path: &str, kind: &str, strict: bool) -> Result<Option<String>> {
    if strict {
        return Err(anyhow!(
            "'{path}' is {kind}, not a regular file; refused because \
             RUNNER_STRICT_PATH_CONFINEMENT is on"
        ));
    }
    let warning = format!(
        "'{path}' is {kind}, not a regular file; allowed because \
         RUNNER_STRICT_PATH_CONFINEMENT is off"
    );
    if first_time(&warning) {
        tracing::warn!("{warning}");
    }
    Ok(Some(warning))
}

/// What a non-regular, non-directory file is, for [`special_file`].
fn special_kind(meta: &std::fs::Metadata) -> &'static str {
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileTypeExt;
        let kind = meta.file_type();
        if kind.is_char_device() {
            return "a character device";
        }
        if kind.is_block_device() {
            return "a block device";
        }
        if kind.is_fifo() {
            return "a named pipe (FIFO)";
        }
        if kind.is_socket() {
            return "a socket";
        }
    }
    let _ = meta;
    "a device, pipe or socket"
}

/// What an opened handle expected to hold a file is, from its fstat: a folder
/// is refused in every mode, a device, FIFO or socket is [`special_file`].
fn opened_file_kind(meta: &std::fs::Metadata, path: &str, strict: bool) -> Result<Option<String>> {
    if meta.is_dir() {
        return Err(anyhow!("'{path}' is a directory, not a file"));
    }
    if !meta.is_file() {
        return special_file(path, special_kind(meta), strict);
    }
    Ok(None)
}

/// Whether an open handle is a Windows device: a character device or a pipe
/// (`NUL`, a serial port, the console, a named pipe). Windows cannot describe
/// such a handle - [`File::metadata`] fails on it - but its file type still
/// tells it from a file on a volume.
#[cfg(windows)]
fn device_handle(file: &File) -> bool {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{GetFileType, FILE_TYPE_CHAR, FILE_TYPE_PIPE};
    // SAFETY: `file` owns the handle and keeps it open for the call.
    let kind = unsafe { GetFileType(file.as_raw_handle() as _) };
    kind == FILE_TYPE_CHAR || kind == FILE_TYPE_PIPE
}

#[cfg(not(windows))]
fn device_handle(_file: &File) -> bool {
    false
}

/// What an opened handle that cannot describe itself ([`File::metadata`]
/// fails) is, whatever the path's name: a Windows device ([`device_handle`]),
/// or a volume or disk opened raw (`\\.\C:`, `\\.\PhysicalDrive0`), which
/// Windows answers no file query for. Neither is a regular file, so
/// [`special_file`].
fn undescribed_handle_kind(file: &File, path: &str, strict: bool) -> Result<Option<String>> {
    let kind = if device_handle(file) {
        "a Windows device"
    } else {
        "a raw volume or disk"
    };
    special_file(path, kind, strict)
}

/// Open `path` for an unconfined read or write the way a plain open did,
/// without the one wait no caller can get out of: non-blocking (a FIFO with no
/// writer, or no reader for a write, returns instead of waiting forever), then
/// judged by the handle it got ([`opened_file_kind`]: a file swapped for a FIFO
/// or a device after the first check is seen here too), then set back to
/// blocking, so reading or writing it behaves as before. A write creates the
/// file and truncates a regular file only once the handle is judged. A handle
/// that cannot describe itself - a Windows device, a raw volume or disk - is
/// no regular file either ([`undescribed_handle_kind`]).
fn open_unconfined(path: &str, write: bool, strict: bool) -> Result<(File, Option<String>)> {
    let mut options = OpenOptions::new();
    if write {
        options.write(true).create(true).truncate(false);
    } else {
        options.read(true);
    }
    set_nonblocking(&mut options);
    let file = options
        .open(path)
        .with_context(|| format!("opening '{path}'"))?;
    let meta = file.metadata().ok();
    let warning = match &meta {
        Some(meta) => opened_file_kind(meta, path, strict)?,
        None => undescribed_handle_kind(&file, path, strict)?,
    };
    set_blocking(&file);
    if write && meta.as_ref().is_some_and(|meta| meta.is_file()) {
        file.set_len(0)
            .with_context(|| format!("truncating '{path}'"))?;
    }
    Ok((file, warning))
}

/// `path` the way the file calls read it: from the working directory when
/// relative, without `.` segments or doubled separators. `None` with a `..`
/// segment, which the backend's check refuses too.
fn absolute(path: &Path) -> Option<PathBuf> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().ok()?.join(path)
    };
    if path.components().any(|part| part == Component::ParentDir) {
        return None;
    }
    Some(
        path.components()
            .filter(|part| *part != Component::CurDir)
            .collect(),
    )
}

/// A share, a device namespace or an automount root (`\\server\share`,
/// `\\?\...`, `/net/host`): merely looking one up can reach another machine.
fn is_remote(path: &Path) -> bool {
    path.to_str()
        .is_some_and(|text| local_paths::check_local_shape(text) == Err(LocalPathError::Remote))
}

/// A remote path is only looked up inside a root written the same way (an
/// allowed share); anywhere else it is refused before any file-system call.
fn may_look_up(path: &Path, roots: &[PathBuf]) -> bool {
    !is_remote(path)
        || roots
            .iter()
            .any(|root| is_remote(root) && within_as_written(path, root))
}

/// Whether `path` is `root` or lies below it, component by component; case
/// and separators are folded on Windows, as the backend's check does, except
/// inside a folder Windows keeps case sensitive (WSL, `fsutil file
/// setCaseSensitiveInfo`), where names that differ only in case are two
/// different entries.
fn within(path: &Path, root: &Path) -> bool {
    within_by(path, root, case_sensitive_folder)
}

/// [`within`] as plain string logic, touching no file system.
fn within_as_written(path: &Path, root: &Path) -> bool {
    within_by(path, root, |_| false)
}

fn within_by(path: &Path, root: &Path, case_sensitive: impl Fn(&Path) -> bool) -> bool {
    let mut parts = path.components();
    let mut folder = PathBuf::new();
    root.components().all(|part| {
        let same = parts.next().is_some_and(|other| {
            part == other
                || (same_component(part, other)
                    && !(matches!(part, Component::Normal(_)) && case_sensitive(&folder)))
        });
        folder.push(part);
        same
    })
}

/// Whether Windows keeps `folder` case sensitive; `false` where it cannot
/// tell (no such folder, a file system without the setting).
#[cfg(windows)]
fn case_sensitive_folder(folder: &Path) -> bool {
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        FileCaseSensitiveInfo, GetFileInformationByHandleEx, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_READ_ATTRIBUTES,
    };
    use windows_sys::Win32::System::WindowsProgramming::FILE_CASE_SENSITIVE_INFO;
    // FILE_CS_FLAG_CASE_SENSITIVE_DIR (winnt.h).
    const CASE_SENSITIVE_DIR: u32 = 1;
    let Ok(handle) = OpenOptions::new()
        .access_mode(FILE_READ_ATTRIBUTES)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
        .open(folder)
    else {
        return false;
    };
    let mut info = FILE_CASE_SENSITIVE_INFO { Flags: 0 };
    // SAFETY: `info` is the FILE_CASE_SENSITIVE_INFO the call fills, of the
    // size passed, and `handle` stays open for the call.
    let read = unsafe {
        GetFileInformationByHandleEx(
            handle.as_raw_handle(),
            FileCaseSensitiveInfo,
            (&mut info as *mut FILE_CASE_SENSITIVE_INFO).cast(),
            std::mem::size_of::<FILE_CASE_SENSITIVE_INFO>() as u32,
        )
    };
    read != 0 && info.Flags & CASE_SENSITIVE_DIR != 0
}

#[cfg(not(windows))]
fn case_sensitive_folder(_folder: &Path) -> bool {
    false
}

fn same_component(a: Component, b: Component) -> bool {
    if !cfg!(windows) {
        return a == b;
    }
    let fold = |part: Component| {
        part.as_os_str()
            .to_string_lossy()
            .replace('/', "\\")
            .to_lowercase()
    };
    fold(a) == fold(b)
}

/// Where `path` (absolute) leads on this machine, links followed: its
/// canonical form when it exists; otherwise the canonical form of its nearest
/// existing ancestor plus the names below it, where a write creates them.
/// `None` when that cannot be told.
fn resolve(path: &Path) -> Option<PathBuf> {
    let mut anchor = path.to_path_buf();
    let mut below = Vec::new();
    let mut hops = 0;
    loop {
        let Ok(meta) = std::fs::symlink_metadata(&anchor) else {
            below.push(anchor.file_name()?.to_os_string());
            anchor = anchor.parent()?.to_path_buf();
            continue;
        };
        let found = match std::fs::canonicalize(&anchor) {
            Ok(canonical) => local_paths::shell_form(canonical),
            // A link to nothing yet: a write lands at its target.
            Err(_) if meta.file_type().is_symlink() && hops < MAX_LINK_HOPS => {
                hops += 1;
                let target = std::fs::read_link(&anchor).ok()?;
                anchor = anchor.parent()?.join(target);
                continue;
            }
            // Some Windows volumes cannot name their files (RAM disks,
            // VirtualBox shared folders): there a path with no link and no
            // `..` on it leads where it says.
            Err(_) if leads_where_it_says(&anchor) => anchor,
            Err(_) => return None,
        };
        return Some(below.iter().rev().fold(found, |path, name| path.join(name)));
    }
}

fn leads_where_it_says(path: &Path) -> bool {
    !path
        .components()
        .any(|part| part == Component::ParentDir || only_dots_and_spaces(part))
        && !path.ancestors().any(|step| {
            std::fs::symlink_metadata(step).is_ok_and(|meta| meta.file_type().is_symlink())
        })
}

/// A Windows name made only of dots and spaces (`.. `, `...`): Windows trims
/// such a name at the end of a path, so it does not read as it is written.
fn only_dots_and_spaces(part: Component) -> bool {
    cfg!(windows)
        && matches!(part, Component::Normal(name)
            if name.to_str().is_some_and(|name| name.chars().all(|c| c == '.' || c == ' ')))
}

fn outside(path: &str) -> anyhow::Error {
    anyhow!(
        "'{path}' is outside the runner's allowed folders once links are resolved. To reach \
         where it leads, the user can add that folder to the runner's allowed paths on the \
         Desktop page (sidebar > XTM One Desktop > this machine)"
    )
}

/// A confined command: where `path` leads once links are followed
/// ([`resolve`], the roots too). `None` when that is inside the roots; when it
/// is not, see [`leaves`].
fn confine(path: &str, confinement: &Confinement) -> Result<Option<String>> {
    let written = absolute(Path::new(path))
        .ok_or_else(|| anyhow!("'{path}' must not contain '..' segments"))?;
    if !may_look_up(&written, &confinement.roots) {
        return Err(outside(path));
    }
    match resolve(&written) {
        Some(landing) if within_roots(&landing, &confinement.roots) => Ok(None),
        landing => leaves(path, landing.as_deref(), confinement),
    }
}

/// `path` leads outside the allowed folders (to `landing`), or where it leads
/// cannot be told: refused in strict mode; otherwise it runs, as it always
/// did, and this is the warning its result carries.
fn leaves(path: &str, landing: Option<&Path>, confinement: &Confinement) -> Result<Option<String>> {
    if confinement.strict {
        return Err(outside(path));
    }
    let warning = match landing {
        Some(landing) => format!(
            "'{path}' leads outside the runner's allowed folders through a link (to '{}'); \
             allowed because RUNNER_STRICT_PATH_CONFINEMENT is off",
            landing.display()
        ),
        None => format!(
            "where '{path}' leads could not be checked against the runner's allowed folders; \
             allowed because RUNNER_STRICT_PATH_CONFINEMENT is off"
        ),
    };
    if first_time(&warning) {
        tracing::warn!("{warning}");
    }
    Ok(Some(warning))
}

/// Whether this process has not logged `warning` yet; it is remembered now.
fn first_time(warning: &str) -> bool {
    static LOGGED: std::sync::OnceLock<std::sync::Mutex<std::collections::HashSet<String>>> =
        std::sync::OnceLock::new();
    let Ok(mut logged) = LOGGED.get_or_init(Default::default).lock() else {
        return true;
    };
    if logged.len() >= LOGGED_WARNINGS {
        logged.clear();
    }
    logged.insert(warning.to_string())
}

/// Keep BOTH warnings in the one `warning` field a result carries: every
/// distinct line once, in the order met, so a merged warning merged again (the
/// handle check returns its own two lines) is not doubled. `Option::or` kept
/// only the first, so a path that earned several warnings - a link that leads
/// outside the allowed folders AND onto a device or FIFO, say - told the agent
/// about only the first and hid the rest. Each message is still logged
/// where it is produced ([`special_file`], [`leaves`]), so the log already
/// carries them all; this is the result side.
fn add_warning(warning: Option<String>, next: Option<String>) -> Option<String> {
    let mut lines: Vec<&str> = Vec::new();
    for line in warning.iter().chain(next.iter()).flat_map(|w| w.lines()) {
        if !lines.contains(&line) {
            lines.push(line);
        }
    }
    (!lines.is_empty()).then(|| lines.join("\n"))
}

/// Whether `path` (already resolved) is inside one of `roots`, each root
/// resolved the same way. The single comparison [`confine`] and the post-open
/// handle check ([`verify_opened`]) share, so the check before the open and the
/// one after it can never disagree on where a root ends.
fn within_roots(path: &Path, roots: &[PathBuf]) -> bool {
    roots
        .iter()
        .any(|root| resolve(root).is_some_and(|root| within(path, &root)))
}

/// What a confined command expected to open.
#[derive(Clone, Copy)]
enum Opened {
    File,
    Dir,
}

/// After a confined command opens its object, confirm FROM THE HANDLE what it
/// is: the kind is an fstat on the handle ([`File::metadata`]), so a device
/// swapped in for a file is seen here (a folder where a file was expected, or
/// the reverse, is refused; a device, FIFO or socket is [`special_file`]) -
/// and where it really is: the handle's own final path
/// ([`handle_final_path`]), held to the roots by [`within_roots`] exactly as
/// the pre-open check holds the path, with the same outcome ([`leaves`]).
/// When the OS cannot name the open file, the pre-open check stands - the same
/// fallback [`resolve`] takes for a volume that cannot resolve paths. A handle
/// that cannot describe itself - a Windows device, a raw volume or disk - is
/// no regular file ([`undescribed_handle_kind`]) and never a folder.
fn verify_opened(
    file: &File,
    path: &str,
    confinement: &Confinement,
    expected: Opened,
) -> Result<Option<String>> {
    let meta = file.metadata().ok();
    let kind = match (expected, &meta) {
        (Opened::File, Some(meta)) => opened_file_kind(meta, path, confinement.strict)?,
        (Opened::File, None) => undescribed_handle_kind(file, path, confinement.strict)?,
        (Opened::Dir, Some(meta)) if meta.is_dir() => None,
        (Opened::Dir, _) => {
            return Err(anyhow!("'{path}' is not a directory; use read_file"));
        }
    };
    let located = match handle_final_path(file) {
        Some(real) if !within_roots(&real, &confinement.roots) => {
            leaves(path, Some(&real), confinement)?
        }
        _ => None,
    };
    Ok(add_warning(kind, located))
}

/// Open a directory handle to verify a confined `list_dir`: on Windows a folder
/// opens only with backup semantics, elsewhere a plain read handle names it.
/// Non-blocking so a FIFO swapped onto the path returns instead of hanging the
/// open; the handle is refused by its type straight after.
fn open_dir(path: &str) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    set_nonblocking(&mut options);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_BACKUP_SEMANTICS;
        options.custom_flags(FILE_FLAG_BACKUP_SEMANTICS);
    }
    options
        .open(path)
        .with_context(|| format!("opening '{path}'"))
}

/// Open a confined file for reading, non-blocking (a FIFO swapped onto the path
/// returns instead of hanging the open); the handle is verified straight after.
fn open_confined_read(path: &str) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    set_nonblocking(&mut options);
    options
        .open(path)
        .with_context(|| format!("opening '{path}'"))
}

/// Ask the open not to block on a FIFO or device (Unix `O_NONBLOCK`): a regular
/// file and a directory ignore it for I/O, but a FIFO swapped onto the path
/// would otherwise wait for its other end and hang the runner instead of being
/// refused. No-op where the concern does not arise (Windows named pipes cannot
/// be placed at a file-system path inside an allowed folder).
#[cfg(unix)]
fn set_nonblocking(options: &mut OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;
    options.custom_flags(libc::O_NONBLOCK);
}

#[cfg(not(unix))]
fn set_nonblocking(_options: &mut OpenOptions) {}

/// Undo [`set_nonblocking`] on an open handle, once it is known to be allowed:
/// a device or FIFO then reads and writes as a plain open would (a no-op on a
/// regular file).
#[cfg(unix)]
fn set_blocking(file: &File) {
    use std::os::unix::io::AsRawFd;
    let fd = file.as_raw_fd();
    // SAFETY: `fd` is an open descriptor owned by `file` for these calls.
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFL);
        if flags >= 0 {
            libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
        }
    }
}

#[cfg(not(unix))]
fn set_blocking(_file: &File) {}

/// The real path an open handle resolves to on this machine, in the same shell
/// form [`resolve`] produces (no `\\?\` on Windows), or `None` when the OS cannot
/// name it (`/proc` not mounted, a volume without path resolution, a file
/// unlinked after it was opened) - the fallback then leaves the pre-open check
/// standing.
#[cfg(target_os = "linux")]
fn handle_final_path(file: &File) -> Option<PathBuf> {
    use std::os::unix::io::AsRawFd;
    let real = std::fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd())).ok()?;
    // A file unlinked after opening reads back as "<path> (deleted)": unknowable.
    if real.as_os_str().to_string_lossy().ends_with(" (deleted)") {
        return None;
    }
    Some(real)
}

#[cfg(target_os = "macos")]
fn handle_final_path(file: &File) -> Option<PathBuf> {
    use std::os::unix::ffi::OsStringExt;
    use std::os::unix::io::AsRawFd;
    let mut buf = [0u8; libc::PATH_MAX as usize];
    // SAFETY: `buf` is PATH_MAX bytes, the size F_GETPATH writes the path into.
    let rc = unsafe {
        libc::fcntl(
            file.as_raw_fd(),
            libc::F_GETPATH,
            buf.as_mut_ptr() as *mut libc::c_char,
        )
    };
    if rc != 0 {
        return None;
    }
    let len = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    if len == 0 {
        return None;
    }
    Some(PathBuf::from(std::ffi::OsString::from_vec(
        buf[..len].to_vec(),
    )))
}

#[cfg(windows)]
fn handle_final_path(file: &File) -> Option<PathBuf> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::GetFinalPathNameByHandleW;
    let handle = file.as_raw_handle();
    // First call sizes the buffer (WCHARs, including the terminator).
    // SAFETY: a null buffer of length 0 asks only for the required size.
    let needed = unsafe { GetFinalPathNameByHandleW(handle as _, std::ptr::null_mut(), 0, 0) };
    if needed == 0 {
        return None;
    }
    let mut buf = vec![0u16; needed as usize];
    // SAFETY: `buf` holds `needed` WCHARs; flags 0 = normalized DOS path.
    let written = unsafe { GetFinalPathNameByHandleW(handle as _, buf.as_mut_ptr(), needed, 0) };
    if written == 0 || written >= needed {
        return None;
    }
    let text = String::from_utf16(&buf[..written as usize]).ok()?;
    Some(local_paths::shell_form(PathBuf::from(text)))
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn handle_final_path(_file: &File) -> Option<PathBuf> {
    None
}

/// A completed command's result, with the confinement warning it earned.
fn with_warning(mut result: serde_json::Value, warning: Option<String>) -> serde_json::Value {
    if let (Some(warning), Some(fields)) = (warning, result.as_object_mut()) {
        fields.insert("warning".to_string(), warning.into());
    }
    result
}

/// The optional `max_inline_bytes`: how much a read may return when it comes
/// back as inline text, where `max_bytes` - the upload limit a file result
/// must fit - is lower (inline text never meets that limit). Never past the
/// inline size; ignored when it asks for no more than `max_bytes`.
fn max_inline_bytes(payload: &serde_json::Value, max_bytes: u64) -> Option<u64> {
    payload
        .get("max_inline_bytes")
        .and_then(|v| v.as_u64())
        .map(|inline| inline.min(INLINE_TEXT_LIMIT as u64))
        .filter(|inline| *inline > max_bytes)
}

pub fn read_file(payload: &serde_json::Value) -> CommandOutcome {
    match read_inner(payload) {
        Ok(outcome) => outcome,
        Err(err) => CommandOutcome::failed(format!("read_file failed: {err:#}")),
    }
}

fn read_inner(payload: &serde_json::Value) -> Result<CommandOutcome> {
    let path = payload
        .get("path")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'path'"))?;
    let max_bytes = payload
        .get("max_bytes")
        .and_then(|v| v.as_u64())
        .unwrap_or(DEFAULT_READ_LIMIT);
    let confinement = confinement(payload);
    let mut warning = match &confinement {
        Some(confinement) => confine(path, confinement)?,
        None => None,
    };

    let meta = std::fs::metadata(path).with_context(|| format!("stat '{path}'"))?;
    if meta.is_dir() {
        return Err(anyhow!("'{path}' is a directory; use list_dir"));
    }
    let special = !meta.is_file();
    if special {
        warning = add_warning(
            warning,
            special_file(path, special_kind(&meta), strict_requested(payload))?,
        );
    }
    // Read through a bounded reader so a multi-gigabyte file cannot be slurped
    // into memory before the ceiling is applied - cap the ALLOCATION, not just
    // the returned slice (std::fs::read would allocate the whole file first).
    use std::io::Read;
    // Confined: open without blocking (a FIFO swapped in for the file would
    // otherwise hang the open) and confirm the handle we hold is the file we
    // vetted, not a link swapped in after the check.
    let file = match &confinement {
        Some(confinement) => {
            let file = open_confined_read(path)?;
            warning = add_warning(
                warning,
                verify_opened(&file, path, confinement, Opened::File)?,
            );
            set_blocking(&file);
            file
        }
        None => {
            let (file, kind) = open_unconfined(path, false, strict_requested(payload))?;
            warning = add_warning(warning, kind);
            file
        }
    };
    let max_inline = max_inline_bytes(payload, max_bytes);
    let read_limit = max_inline.unwrap_or(max_bytes);
    let mut raw = Vec::new();
    // One byte past the ceiling tells a file that goes on from one that fits
    // it exactly; that byte is never returned.
    file.take(read_limit.saturating_add(1))
        .read_to_end(&mut raw)
        .with_context(|| format!("reading '{path}'"))?;
    let mut truncated = raw.len() as u64 > read_limit;
    raw.truncate(usize::try_from(read_limit).unwrap_or(usize::MAX));
    let inline_text = std::str::from_utf8(&raw)
        .ok()
        .filter(|text| text.len() <= INLINE_TEXT_LIMIT)
        .map(str::to_owned);

    match inline_text {
        Some(text) => {
            let mut result = serde_json::json!({
                "path": path,
                "content": text,
                "size": raw.len(),
                "truncated": truncated,
                // Tells the backend this runner honours `max_inline_bytes`,
                // whether or not this read needed it: a cut text read may ask
                // for up to this much.
                "inline_text_limit": INLINE_TEXT_LIMIT,
            });
            // The inline allowance this read used.
            if let Some(inline) = max_inline {
                result["max_inline_bytes"] = inline.into();
            }
            Ok(CommandOutcome::completed(with_warning(result, warning)))
        }
        None => {
            // A file result never goes past `max_bytes`, the upload limit.
            if raw.len() as u64 > max_bytes {
                raw.truncate(usize::try_from(max_bytes).unwrap_or(usize::MAX));
                truncated = true;
            }
            let filename = std::path::Path::new(path)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("file.bin")
                .to_string();
            Ok(CommandOutcome::completed_file(
                with_warning(
                    serde_json::json!({
                        "path": path,
                        "size": raw.len(),
                        "encoding": "binary",
                        "truncated": truncated,
                    }),
                    warning,
                ),
                FilePart {
                    bytes: raw,
                    filename,
                    content_type: "application/octet-stream".to_string(),
                },
            ))
        }
    }
}

pub fn write_file(payload: &serde_json::Value) -> CommandOutcome {
    match write_inner(payload) {
        Ok(outcome) => outcome,
        Err(err) => CommandOutcome::failed(format!("write_file failed: {err:#}")),
    }
}

fn write_inner(payload: &serde_json::Value) -> Result<CommandOutcome> {
    let path = payload
        .get("path")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'path'"))?;
    let content = payload
        .get("content")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'content'"))?;
    let confinement = confinement(payload);
    let mut warning = match &confinement {
        Some(confinement) => confine(path, confinement)?,
        None => None,
    };
    let existing = std::fs::metadata(path).ok();
    if existing.as_ref().is_some_and(|meta| meta.is_dir()) {
        return Err(anyhow!("'{path}' is a folder, not a file"));
    }
    let special = existing.as_ref().is_some_and(|meta| !meta.is_file());
    if let Some(meta) = existing.as_ref().filter(|_| special) {
        warning = add_warning(
            warning,
            special_file(path, special_kind(meta), strict_requested(payload))?,
        );
    }

    if let Some(parent) = std::path::Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating parent of '{path}'"))?;
        }
    }
    let warning = match &confinement {
        // Full control: write as before - the mode grants full file access.
        None => {
            use std::io::Write;
            let (mut file, kind) = open_unconfined(path, true, strict_requested(payload))?;
            file.write_all(content.as_bytes())
                .with_context(|| format!("writing '{path}'"))?;
            add_warning(warning, kind)
        }
        // Confined: open without truncating, confirm the handle is a regular file
        // (in strict mode, inside the allowed folders too), and only then
        // truncate and write, so a link swapped in after the check can neither
        // redirect the write nor leave the real file emptied on refusal.
        Some(confinement) => add_warning(warning, confined_write(path, content, confinement)?),
    };
    Ok(CommandOutcome::completed(with_warning(
        serde_json::json!({
            "path": path,
            "bytes_written": content.len(),
        }),
        warning,
    )))
}

/// Write `content` to `path` under `confinement`, verifying the opened handle
/// before any truncation. An existing file is left untouched on refusal (it is
/// never truncated before the check); a file this call created that a post-check
/// swap landed outside is removed best-effort so a refused create leaves no
/// residue. The one narrow case it cannot fully prevent - a create racing a swap
/// to an outside dangling link on an OS that cannot name the open handle - is
/// documented in `docs/docs/usage/resources/runners.md`.
fn confined_write(path: &str, content: &str, confinement: &Confinement) -> Result<Option<String>> {
    use std::io::Write;
    let pre_existing = std::fs::metadata(path).is_ok();
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(false);
    set_nonblocking(&mut options);
    let mut file = options
        .open(path)
        .with_context(|| format!("opening '{path}' for writing"))?;
    let warning = match verify_opened(&file, path, confinement, Opened::File) {
        Ok(warning) => warning,
        Err(err) => {
            if !pre_existing {
                // We created it this call; if it landed outside and is still the
                // empty file we made, undo that side effect.
                let residue = handle_final_path(&file)
                    .filter(|_| file.metadata().map(|meta| meta.len() == 0).unwrap_or(false));
                drop(file);
                if let Some(residue) = residue {
                    let _ = std::fs::remove_file(&residue);
                }
            }
            return Err(err);
        }
    };
    set_blocking(&file);
    // A device or FIFO has no length to cut (and refuses it); a regular file
    // is truncated only now, once the handle is verified.
    if file.metadata().is_ok_and(|meta| meta.is_file()) {
        file.set_len(0)
            .with_context(|| format!("truncating '{path}'"))?;
    }
    file.write_all(content.as_bytes())
        .with_context(|| format!("writing '{path}'"))?;
    Ok(warning)
}

pub fn list_dir(payload: &serde_json::Value) -> CommandOutcome {
    match list_inner(payload) {
        Ok(outcome) => outcome,
        Err(err) => CommandOutcome::failed(format!("list_dir failed: {err:#}")),
    }
}

fn list_inner(payload: &serde_json::Value) -> Result<CommandOutcome> {
    let path = payload
        .get("path")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("missing 'path'"))?;
    let confinement = confinement(payload);
    let mut warning = None;
    // Confined: list through the directory handle's own resolved path, having
    // confirmed the handle is a directory (in strict mode, inside the allowed
    // folders too) - a link swapped in after the check cannot redirect the
    // listing, and a canonical path has no link components left to swap. Outside strict mode a folder whose handle cannot be opened is
    // listed as it always was.
    let listing: PathBuf = match &confinement {
        Some(confinement) => {
            warning = confine(path, confinement)?;
            match open_dir(path) {
                Ok(dir) => {
                    warning = add_warning(
                        warning,
                        verify_opened(&dir, path, confinement, Opened::Dir)?,
                    );
                    handle_final_path(&dir).unwrap_or_else(|| PathBuf::from(path))
                }
                Err(_) if !confinement.strict => PathBuf::from(path),
                Err(err) => return Err(err),
            }
        }
        None => PathBuf::from(path),
    };

    let mut entries = Vec::new();
    for entry in std::fs::read_dir(&listing).with_context(|| format!("listing '{path}'"))? {
        let entry = match entry {
            Ok(e) => e,
            Err(_) => continue,
        };
        // `DirEntry::metadata` does not follow a link: a link is listed as the
        // link itself, never with the type, size or time of a target that may
        // lie outside the allowed folders.
        let meta = entry.metadata().ok();
        let modified = meta
            .as_ref()
            .and_then(|m| m.modified().ok())
            .and_then(|t| chrono::DateTime::<chrono::Utc>::from(t).to_rfc3339().into());
        entries.push(serde_json::json!({
            "name": entry.file_name().to_string_lossy(),
            "is_dir": meta.as_ref().map(|m| m.is_dir()).unwrap_or(false),
            "is_link": meta.as_ref().is_some_and(|m| m.file_type().is_symlink()),
            "size": meta.as_ref().map(|m| m.len()).unwrap_or(0),
            "modified": modified,
        }));
    }
    entries.sort_by(|a, b| {
        a.get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .cmp(b.get("name").and_then(|v| v.as_str()).unwrap_or(""))
    });

    Ok(CommandOutcome::completed(with_warning(
        serde_json::json!({
            "path": path,
            "entries": entries,
        }),
        warning,
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::OutcomeStatus;
    use serde_json::json;

    /// A fresh scratch folder per test (the test binary runs tests in parallel).
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("xtm-files-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn text(path: &Path) -> String {
        path.to_str().unwrap().to_string()
    }

    /// The result of a completed command; `None` when it was refused or failed.
    fn done(outcome: CommandOutcome) -> Option<serde_json::Value> {
        (outcome.status == OutcomeStatus::Completed).then(|| outcome.result.unwrap_or_default())
    }

    /// A command as the backend delivers it; `roots` is `null` for a runner
    /// without an allowed-paths list, `strict` the operator's opt-in
    /// (`RUNNER_STRICT_PATH_CONFINEMENT`).
    fn command(path: &str, roots: &serde_json::Value, strict: bool) -> serde_json::Value {
        json!({ "path": path, "content": "x", "allowed_roots": roots, "strict_confinement": strict })
    }

    fn read(path: &str, roots: &serde_json::Value) -> Option<serde_json::Value> {
        done(read_file(&command(path, roots, false)))
    }

    fn list(path: &str, roots: &serde_json::Value) -> Option<serde_json::Value> {
        done(list_dir(&command(path, roots, false)))
    }

    fn write(path: &str, roots: &serde_json::Value) -> Option<serde_json::Value> {
        done(write_file(&command(path, roots, false)))
    }

    fn strict_read(path: &str, roots: &serde_json::Value) -> Option<serde_json::Value> {
        done(read_file(&command(path, roots, true)))
    }

    fn strict_list(path: &str, roots: &serde_json::Value) -> Option<serde_json::Value> {
        done(list_dir(&command(path, roots, true)))
    }

    fn strict_write(path: &str, roots: &serde_json::Value) -> Option<serde_json::Value> {
        done(write_file(&command(path, roots, true)))
    }

    /// The confinement warning a completed command carries, if any.
    fn warning(result: &serde_json::Value) -> Option<&str> {
        result.get("warning").and_then(|warning| warning.as_str())
    }

    /// Confinement to `root`, strict or not, as [`confinement`] reads it.
    fn confined_to(root: &Path, strict: bool) -> Confinement {
        Confinement {
            roots: vec![absolute(root).unwrap()],
            strict,
        }
    }

    #[test]
    fn without_allowed_roots_a_path_is_used_as_given() {
        let dir = scratch("unconfined");
        let file = text(&dir.join("notes.txt"));
        std::fs::write(&file, b"hello").unwrap();
        let read_back = read(&file, &json!(null)).unwrap();
        assert_eq!(read_back["content"], "hello");
        assert_eq!(warning(&read_back), None);
        assert_eq!(write(&file, &json!(null)).unwrap()["path"], file);
        assert_eq!(list(&text(&dir), &json!(null)).unwrap()["path"], text(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A read with a size ceiling, as the backend delivers it.
    fn read_up_to(path: &str, max_bytes: u64, roots: &serde_json::Value) -> CommandOutcome {
        read_file(&json!({ "path": path, "max_bytes": max_bytes, "allowed_roots": roots }))
    }

    #[test]
    fn a_read_says_whether_the_file_goes_on_past_max_bytes() {
        let dir = scratch("read-cut");
        let file = text(&dir.join("notes.txt"));
        std::fs::write(&file, b"hello world").unwrap();
        for roots in [json!(null), json!([text(&dir)])] {
            let cut = done(read_up_to(&file, 5, &roots)).unwrap();
            assert_eq!(cut["content"], "hello");
            assert_eq!(cut["size"], 5);
            assert_eq!(cut["truncated"], true);
            // Exactly the file's size fits it: nothing is left unread.
            for fits in [11, 12, DEFAULT_READ_LIMIT, u64::MAX] {
                let whole = done(read_up_to(&file, fits, &roots)).unwrap();
                assert_eq!(whole["content"], "hello world", "{fits}");
                assert_eq!(whole["truncated"], false, "{fits}");
            }
            let nothing = done(read_up_to(&file, 0, &roots)).unwrap();
            assert_eq!(nothing["size"], 0);
            assert_eq!(nothing["truncated"], true);
        }
        let empty = text(&dir.join("empty.txt"));
        std::fs::write(&empty, b"").unwrap();
        assert_eq!(
            done(read_up_to(&empty, 0, &json!(null))).unwrap()["truncated"],
            false
        );
        assert_eq!(read(&empty, &json!(null)).unwrap()["truncated"], false);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_file_result_cut_at_max_bytes_carries_those_bytes_only() {
        let dir = scratch("read-cut-file");
        let file = text(&dir.join("big.log"));
        let ceiling = INLINE_TEXT_LIMIT + 10;
        std::fs::write(&file, vec![b'a'; ceiling + 100]).unwrap();

        let cut = read_up_to(&file, ceiling as u64, &json!(null));
        assert_eq!(
            cut.file.as_ref().map(|part| part.bytes.len()),
            Some(ceiling)
        );
        let cut = done(cut).unwrap();
        assert_eq!(cut["size"], ceiling);
        assert_eq!(cut["truncated"], true);

        let whole = read_up_to(&file, (ceiling + 100) as u64, &json!(null));
        assert_eq!(
            whole.file.as_ref().map(|part| part.bytes.len()),
            Some(ceiling + 100)
        );
        assert_eq!(done(whole).unwrap()["truncated"], false);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_largest_inline_read_fits_a_result_upload() {
        use crate::protocol::{ResultMeta, MAX_RESULT_META_BYTES};

        // Control characters are the worst case for the JSON the platform
        // reads: each one is written `\u00XX`, six bytes.
        let dir = scratch("read-inline-worst");
        let file = text(&dir.join("control.txt"));
        std::fs::write(&file, vec![0x01; INLINE_TEXT_LIMIT]).unwrap();
        let result = read(&file, &json!(null)).unwrap();
        assert_eq!(result["size"], INLINE_TEXT_LIMIT);
        let meta = serde_json::to_string(&ResultMeta {
            status: "completed".into(),
            result: Some(result),
            error: None,
        })
        .unwrap();
        assert!(meta.len() > 6 * INLINE_TEXT_LIMIT, "{}", meta.len());
        assert!(meta.len() <= MAX_RESULT_META_BYTES, "{}", meta.len());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn under_a_small_upload_limit_a_file_result_is_cut_and_text_stays_whole() {
        // `max_bytes` is the upload limit; the optional `max_inline_bytes` is
        // how much an inline text result may hold. A file result is cut at
        // `max_bytes` and flagged, never larger; text up to `max_inline_bytes`
        // comes back whole. Without the field a read is what it always was.
        let dir = scratch("inline-allowance");
        let notes = text(&dir.join("notes.txt"));
        std::fs::write(&notes, b"hello world").unwrap();
        let blob = text(&dir.join("blob.bin"));
        std::fs::write(&blob, [0xffu8; 40]).unwrap();
        let big = text(&dir.join("big.log"));
        std::fs::write(&big, vec![b'a'; INLINE_TEXT_LIMIT + 10]).unwrap();
        let read = |path: &str, inline: Option<u64>| {
            let mut payload = json!({ "path": path, "max_bytes": 5 });
            if let Some(inline) = inline {
                payload["max_inline_bytes"] = json!(inline);
            }
            read_file(&payload)
        };

        let whole = done(read(&notes, Some(100))).unwrap();
        assert_eq!(whole["content"], "hello world");
        assert_eq!(whole["truncated"], false);
        assert_eq!(whole["max_inline_bytes"], 100);
        assert_eq!(whole["inline_text_limit"], INLINE_TEXT_LIMIT);
        let cut = done(read(&notes, Some(8))).unwrap();
        assert_eq!(
            (cut["content"].as_str(), cut["truncated"].as_bool()),
            (Some("hello wo"), Some(true))
        );
        let at_inline = done(read(&big, Some(u64::MAX))).unwrap();
        assert_eq!(at_inline["size"], INLINE_TEXT_LIMIT);
        assert_eq!(at_inline["truncated"], true);

        let file = read(&blob, Some(100));
        assert_eq!(file.file.as_ref().map(|part| part.bytes.len()), Some(5));
        let file = done(file).unwrap();
        assert_eq!(
            (file["size"].as_u64(), file["truncated"].as_bool()),
            (Some(5), Some(true))
        );
        assert!(file.get("max_inline_bytes").is_none());
        assert!(file.get("inline_text_limit").is_none());

        // Absent, or asking for less than max_bytes: exactly as before. The
        // inline result still says how much text this runner returns inline,
        // so the platform knows a cut text read may ask for more.
        for inline in [None, Some(3)] {
            let before = done(read(&notes, inline)).unwrap();
            assert_eq!(before["content"], "hello");
            assert_eq!(before["truncated"], true);
            assert!(before.get("max_inline_bytes").is_none());
            assert_eq!(before["inline_text_limit"], INLINE_TEXT_LIMIT);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_regular_path_inside_an_allowed_root_is_read_listed_and_written() {
        let dir = scratch("inside");
        let root = dir.join("root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("a.txt"), b"a").unwrap();
        let roots = json!([text(&root)]);

        // Results name the path as the caller wrote it, never a resolved form,
        // and carry no warning, strict or not.
        let file = text(&root.join("a.txt"));
        for result in [
            read(&file, &roots),
            strict_read(&file, &roots),
            list(&text(&root), &roots),
            strict_list(&text(&root), &roots),
            write(&file, &roots),
            strict_write(&file, &roots),
        ] {
            let result = result.unwrap();
            assert!(result["path"] == file || result["path"] == text(&root));
            assert_eq!(warning(&result), None);
        }
        assert_eq!(std::fs::read_to_string(root.join("a.txt")).unwrap(), "x");
        let new_file = root.join("sub").join("new.txt");
        assert!(strict_write(&text(&new_file), &roots).is_some());
        assert!(new_file.is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_path_outside_every_allowed_root_is_refused_only_when_strict() {
        let dir = scratch("outside");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        std::fs::write(other.join("b.txt"), b"b").unwrap();
        let roots = json!([text(&root)]);

        assert!(strict_read(&text(&other.join("b.txt")), &roots).is_none());
        assert!(strict_list(&text(&other), &roots).is_none());
        assert!(strict_write(&text(&other.join("c.txt")), &roots).is_none());
        assert!(!other.join("c.txt").exists());
        // The strict refusal says how the user can allow a folder.
        let refusal = read_file(&command(&text(&other.join("b.txt")), &roots, true))
            .error
            .unwrap();
        assert!(refusal.contains("allowed paths"), "{refusal}");
        // The default runs it as before (the backend refuses such a path as
        // written anyway), and says so.
        let read_back = read(&text(&other.join("b.txt")), &roots).unwrap();
        assert_eq!(read_back["content"], "b");
        assert!(warning(&read_back)
            .unwrap()
            .contains("RUNNER_STRICT_PATH_CONFINEMENT"));
        assert!(warning(&list(&text(&other), &roots).unwrap()).is_some());
        assert!(warning(&write(&text(&other.join("c.txt")), &roots).unwrap()).is_some());
        assert!(other.join("c.txt").is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_folder_is_never_written_as_a_file() {
        let dir = scratch("folder-write");
        for roots in [json!(null), json!([text(&dir)])] {
            for strict in [false, true] {
                let refusal = write_file(&command(&text(&dir), &roots, strict))
                    .error
                    .unwrap();
                assert!(refusal.contains("is a folder"), "{refusal}");
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_backend_that_sends_no_strict_flag_gets_the_default() {
        // A server from before the opt-in sends the folders only.
        let dir = scratch("no-flag");
        std::fs::write(dir.join("a.txt"), b"a").unwrap();
        let payload = json!({
            "path": text(&dir.join("a.txt")),
            "allowed_roots": [text(&dir.join("root"))],
        });
        let out = done(read_file(&payload)).unwrap();
        assert_eq!(out["content"], "a");
        assert!(warning(&out).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_warning_is_logged_the_first_time_only() {
        let said = format!("a warning of this test only {}", std::process::id());
        assert!(first_time(&said));
        assert!(!first_time(&said));
    }

    #[test]
    fn every_applicable_warning_is_kept_not_just_the_first() {
        // The result carries one `warning` field, but a path can earn several
        // warnings at once (leads outside the roots AND is a device); `add_warning`
        // keeps them all, an exact repeat once. `Option::or` kept only the first,
        // which hid the rest from the agent.
        assert_eq!(add_warning(None, None), None);
        assert_eq!(add_warning(Some("a".into()), None).as_deref(), Some("a"));
        assert_eq!(add_warning(None, Some("b".into())).as_deref(), Some("b"));
        assert_eq!(
            add_warning(Some("leads outside".into()), Some("a device".into())).as_deref(),
            Some("leads outside\na device"),
        );
        // An exact repeat (the pre-open and handle-time checks say the same
        // thing) is not doubled.
        assert_eq!(
            add_warning(Some("a device".into()), Some("a device".into())).as_deref(),
            Some("a device"),
        );
        // A third distinct message joins the earlier two; a repeat of either is
        // still dropped.
        let two = add_warning(Some("leads outside".into()), Some("a device".into()));
        assert_eq!(
            add_warning(two.clone(), Some("a socket".into())).as_deref(),
            Some("leads outside\na device\na socket"),
        );
        assert_eq!(add_warning(two.clone(), Some("a device".into())), two);
        // A merged warning merged again - the handle check returns its own
        // `kind` + `located` lines, which the read merges with the pre-open
        // ones - keeps each line once, whatever the order.
        let at_open = add_warning(Some("a device".into()), Some("leads outside".into()));
        assert_eq!(
            add_warning(Some("leads outside".into()), at_open).as_deref(),
            Some("leads outside\na device"),
        );
        assert_eq!(
            add_warning(
                Some("a device\nleads outside".into()),
                Some("leads outside\na device".into())
            )
            .as_deref(),
            Some("a device\nleads outside"),
        );
    }

    #[test]
    fn allowed_roots_that_are_not_a_list_confine_to_nothing() {
        let dir = scratch("malformed-roots");
        let file = text(&dir.join("a.txt"));
        std::fs::write(&file, b"a").unwrap();
        // `null` is no confinement, like an absent key; any other value that is
        // not a list confines to no folder at all: every path leads outside.
        assert_eq!(warning(&read(&file, &json!(null)).unwrap()), None);
        for malformed in [
            json!(text(&dir)),
            json!({ "root": text(&dir) }),
            json!(true),
            json!([]),
        ] {
            assert!(strict_read(&file, &malformed).is_none(), "{malformed}");
            assert!(
                strict_list(&text(&dir), &malformed).is_none(),
                "{malformed}"
            );
            assert!(strict_write(&file, &malformed).is_none(), "{malformed}");
            assert!(
                warning(&read(&file, &malformed).unwrap()).is_some(),
                "{malformed}"
            );
        }
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "a");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_confined_path_is_read_as_the_backend_reads_it() {
        let dir = scratch("dots");
        std::fs::write(dir.join("a.txt"), b"a").unwrap();
        let roots = json!([text(&dir)]);
        for spelled in [
            format!("{}/./a.txt", text(&dir)),
            format!("{}//a.txt", text(&dir)),
        ] {
            assert_eq!(read(&spelled, &roots).unwrap()["path"], spelled);
        }
        // `..` is refused whatever the mode, as the backend's check does.
        let name = dir.file_name().unwrap().to_str().unwrap();
        let dotted = format!("{}/../{name}/a.txt", text(&dir));
        assert!(read(&dotted, &roots).is_none());
        assert!(strict_read(&dotted, &roots).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_relative_path_is_taken_from_the_working_directory() {
        let cwd = std::env::current_dir().unwrap();
        assert_eq!(
            absolute(Path::new("data/a.txt")),
            Some(cwd.join("data").join("a.txt"))
        );
        let base = std::env::temp_dir();
        assert_eq!(
            absolute(&base.join(".").join("a.txt")).unwrap().as_os_str(),
            base.join("a.txt").as_os_str()
        );
        assert_eq!(absolute(&base.join("..").join("a.txt")), None);
    }

    #[test]
    fn a_folder_that_does_not_exist_yet_can_be_written_into() {
        let dir = scratch("missing-root");
        std::fs::write(dir.join("other.txt"), b"o").unwrap();
        let reports = dir.join("out").join("reports");
        let roots = json!([text(&reports)]);

        let new_file = text(&reports.join("2026").join("a.txt"));
        let written = strict_write(&new_file, &roots).unwrap();
        assert_eq!(written["path"], new_file);
        assert_eq!(warning(&written), None);
        assert!(reports.join("2026").join("a.txt").is_file());
        assert!(strict_read(&text(&dir.join("other.txt")), &roots).is_none());
        assert!(strict_write(&text(&dir.join("out").join("b.txt")), &roots).is_none());
        assert!(!dir.join("out").join("b.txt").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn within_matches_whole_components() {
        let base = std::env::temp_dir();
        assert!(within(
            &base.join("audit").join("a.txt"),
            &base.join("audit")
        ));
        assert!(within(&base.join("audit"), &base.join("audit")));
        assert!(!within(
            &base.join("audit-escape").join("a.txt"),
            &base.join("audit")
        ));
        if cfg!(windows) {
            assert!(within(
                Path::new(r"c:\USERS\me\a.txt"),
                Path::new("C:/Users/Me/")
            ));
            assert!(within(
                Path::new("//files/team/a.txt"),
                Path::new(r"\\FILES\team")
            ));
        } else {
            assert!(!within(
                Path::new("/srv/Data/a.txt"),
                Path::new("/srv/data")
            ));
        }
    }

    #[test]
    fn a_remote_path_is_only_looked_up_inside_a_root_written_the_same_way() {
        let (share, file, local, evil) = if cfg!(windows) {
            (
                r"\\FILES\team\docs",
                r"\\files\team\docs\a.txt",
                r"C:\data",
                r"\\evil.invalid\share\a.txt",
            )
        } else {
            (
                "/net/files/export",
                "/net/files/export/a.txt",
                "/srv/data",
                "/net/evil.invalid/a.txt",
            )
        };
        let roots = |list: &[&str]| list.iter().map(PathBuf::from).collect::<Vec<_>>();
        let file = Path::new(file);
        assert!(may_look_up(file, &roots(&[local, share])));
        assert!(!may_look_up(file, &roots(&[local])));
        assert!(!may_look_up(
            file,
            &roots(&[format!("{share}/other").as_str()])
        ));
        // A local path is resolved; where it leads decides. Removable and
        // mounted drives are local.
        assert!(may_look_up(&Path::new(local).join("a.txt"), &[]));
        let mounted: &[&str] = if cfg!(windows) {
            &[r"D:\", r"E:\reports\q3"]
        } else {
            &[
                "/Volumes/USB/x",
                "/media/me/usb/x",
                "/mnt/nas/x",
                "/run/media/me/x",
            ]
        };
        for drive in mounted {
            assert!(!is_remote(Path::new(drive)), "{drive}");
        }
        // Refused before any file-system call, whatever the mode (the backend
        // refuses it as written, and the host cannot resolve anyway).
        assert!(read(evil, &json!([local])).is_none());
        assert!(write(evil, &json!([local])).is_none());
    }

    #[test]
    fn an_empty_regular_file_is_read() {
        let dir = scratch("empty");
        let file = text(&dir.join("empty.txt"));
        std::fs::write(&file, b"").unwrap();
        assert_eq!(read(&file, &json!(null)).unwrap()["size"], 0);
        assert_eq!(read(&file, &json!([text(&dir)])).unwrap()["size"], 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn handle_final_path_names_the_opened_file() {
        let dir = scratch("handle-final");
        let file = dir.join("a.txt");
        std::fs::write(&file, b"a").unwrap();
        let handle = std::fs::File::open(&file).unwrap();
        let real = handle_final_path(&handle).expect("the OS should name an open file here");
        // The same shape `resolve` produces for the existing file.
        assert_eq!(real, resolve(&absolute(&file).unwrap()).unwrap());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_handle_that_resolves_outside_the_roots_warns_or_is_refused_with_no_effect() {
        let dir = scratch("handle-outside");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        let inside = root.join("a.txt");
        let beyond = other.join("b.txt");
        std::fs::write(&inside, b"a").unwrap();
        std::fs::write(&beyond, b"keep").unwrap();

        // A handle on a file inside is accepted in both modes; one on a file
        // outside is refused in strict mode and warned about otherwise, and
        // that outside file is left exactly as it was.
        let inside_handle = std::fs::File::open(&inside).unwrap();
        let outside_handle = std::fs::File::open(&beyond).unwrap();
        for strict in [false, true] {
            let confinement = confined_to(&root, strict);
            let accepted =
                verify_opened(&inside_handle, &text(&inside), &confinement, Opened::File);
            assert_eq!(accepted.unwrap(), None);
        }
        let strict = confined_to(&root, true);
        assert!(verify_opened(&outside_handle, &text(&beyond), &strict, Opened::File).is_err());
        let default = confined_to(&root, false);
        let warned = verify_opened(&outside_handle, &text(&beyond), &default, Opened::File);
        assert!(warned.unwrap().is_some());
        assert_eq!(std::fs::read_to_string(&beyond).unwrap(), "keep");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_directory_handle_is_confined_and_typed() {
        let dir = scratch("handle-dir");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        let (default, strict) = (confined_to(&root, false), confined_to(&root, true));

        let root_handle = open_dir(&text(&root)).unwrap();
        assert_eq!(
            verify_opened(&root_handle, &text(&root), &strict, Opened::Dir).unwrap(),
            None
        );
        let other_handle = open_dir(&text(&other)).unwrap();
        assert!(verify_opened(&other_handle, &text(&other), &strict, Opened::Dir).is_err());
        let warned = verify_opened(&other_handle, &text(&other), &default, Opened::Dir);
        assert!(warned.unwrap().is_some());
        // A file where a directory was expected is refused by type, in every mode.
        let file = root.join("a.txt");
        std::fs::write(&file, b"a").unwrap();
        let file_handle = open_dir(&text(&file)).unwrap();
        for confinement in [&default, &strict] {
            assert!(verify_opened(&file_handle, &text(&file), confinement, Opened::Dir).is_err());
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_zero_size_proc_file_is_read_within_max_bytes() {
        // `/proc` reports a size of 0 and still has content: `max_bytes`, not
        // the reported size, bounds the read.
        let out = done(read_file(
            &json!({ "path": "/proc/self/status", "max_bytes": 8 }),
        ))
        .unwrap();
        assert_eq!(out["size"], 8);
    }

    #[cfg(unix)]
    #[test]
    fn a_listed_link_is_reported_as_the_link_never_as_its_target() {
        use std::os::unix::fs::symlink;
        let dir = scratch("list-links");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(other.join("folder")).unwrap();
        let target = other.join("big.txt");
        std::fs::write(&target, vec![b'x'; 5000]).unwrap();
        let in_2001 = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000_000);
        std::fs::File::options()
            .write(true)
            .open(&target)
            .unwrap()
            .set_modified(in_2001)
            .unwrap();
        symlink(&target, root.join("to-file")).unwrap();
        symlink(other.join("folder"), root.join("to-folder")).unwrap();
        std::fs::write(root.join("plain.txt"), b"abc").unwrap();

        for roots in [json!([text(&root)]), json!(null)] {
            let listed = list(&text(&root), &roots).unwrap();
            assert_eq!(warning(&listed), None);
            let entry = |name: &str| {
                listed["entries"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|entry| entry["name"] == name)
                    .unwrap()
                    .clone()
            };
            // Neither the target's type, nor its size, nor its time.
            for name in ["to-file", "to-folder"] {
                let link = entry(name);
                assert_eq!(link["is_link"], true, "{name}");
                assert_eq!(link["is_dir"], false, "{name}");
                assert_ne!(link["size"], 5000, "{name}");
                assert!(
                    !link["modified"].as_str().unwrap().starts_with("2001"),
                    "{name}"
                );
            }
            let plain = entry("plain.txt");
            assert_eq!(plain["is_link"], false);
            assert_eq!(plain["size"], 3);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn without_allowed_roots_a_link_to_a_file_is_followed() {
        let dir = scratch("unconfined-link");
        std::fs::write(dir.join("a.txt"), b"a").unwrap();
        std::os::unix::fs::symlink(dir.join("a.txt"), dir.join("link.txt")).unwrap();
        let link = text(&dir.join("link.txt"));
        assert_eq!(read(&link, &json!(null)).unwrap()["content"], "a");
        assert!(write(&link, &json!(null)).is_some());
        assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "x");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_link_that_leads_outside_is_followed_with_a_warning_unless_strict() {
        let dir = scratch("link");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        std::fs::write(other.join("b.txt"), b"b").unwrap();
        std::os::unix::fs::symlink(&other, root.join("link")).unwrap();
        let roots = json!([text(&root)]);
        let through = root.join("link");

        assert!(strict_read(&text(&through.join("b.txt")), &roots).is_none());
        assert!(strict_list(&text(&through), &roots).is_none());
        assert!(strict_write(&text(&through.join("c.txt")), &roots).is_none());
        assert!(!other.join("c.txt").exists());

        // The default is what the runner always did: follow it, and say so.
        let read_back = read(&text(&through.join("b.txt")), &roots).unwrap();
        assert_eq!(read_back["content"], "b");
        let said = warning(&read_back).unwrap();
        assert!(
            said.contains("leads outside") && said.contains(&text(&other)),
            "{said}"
        );
        let listed = list(&text(&through), &roots).unwrap();
        assert!(warning(&listed).is_some());
        assert_eq!(listed["entries"][0]["name"], "b.txt");
        assert!(warning(&write(&text(&through.join("c.txt")), &roots).unwrap()).is_some());
        assert!(other.join("c.txt").is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_link_to_a_special_file_outside_a_root_carries_every_warning() {
        // A path inside an allowed root that is a link to a device or FIFO
        // OUTSIDE it earns two distinct warnings: it leads outside the roots,
        // and it is not a regular file. Both must reach the result, not just
        // the first: with `Option::or` the agent was told only that the
        // path led outside and never that it had read a device / pipe.
        use std::os::unix::fs::symlink;
        let dir = scratch("link-to-special");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        let fifo = other.join("pipe");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        // SAFETY: `c` is a valid NUL-terminated path that outlives the call.
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
        symlink(&fifo, root.join("to-fifo")).unwrap();
        symlink("/dev/null", root.join("to-null")).unwrap();
        let roots = json!([text(&root)]);

        // Read through the link to the FIFO: leads-outside AND FIFO.
        let read_fifo = read(&text(&root.join("to-fifo")), &roots).unwrap();
        let said = warning(&read_fifo).unwrap();
        assert!(said.contains("leads outside"), "{said}");
        assert!(said.contains("a named pipe (FIFO)"), "{said}");

        // Write through the link to /dev/null: leads-outside AND device.
        let wrote_null = write(&text(&root.join("to-null")), &roots).unwrap();
        let said = warning(&wrote_null).unwrap();
        assert!(said.contains("leads outside"), "{said}");
        assert!(said.contains("a character device"), "{said}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_link_that_stays_inside_the_allowed_folders_is_followed() {
        use std::os::unix::fs::symlink;
        let dir = scratch("links-inside");
        let root = dir.join("root");
        let second = dir.join("second");
        std::fs::create_dir_all(root.join("real")).unwrap();
        std::fs::create_dir_all(&second).unwrap();
        std::fs::write(root.join("real").join("a.txt"), b"a").unwrap();
        symlink(root.join("real"), root.join("same")).unwrap();
        symlink(&second, root.join("to-second")).unwrap();
        symlink(root.join("real").join("a.txt"), root.join("a-link.txt")).unwrap();
        let roots = json!([text(&root), text(&second)]);

        let read_back = strict_read(&text(&root.join("same").join("a.txt")), &roots).unwrap();
        assert_eq!(warning(&read_back), None);
        assert!(strict_list(&text(&root.join("to-second")), &roots).is_some());
        assert!(strict_write(&text(&root.join("to-second").join("b.txt")), &roots).is_some());
        assert!(second.join("b.txt").is_file());
        // Writing through a link to a file updates that file, as without roots.
        assert!(write(&text(&root.join("a-link.txt")), &roots).is_some());
        assert_eq!(
            std::fs::read_to_string(root.join("real").join("a.txt")).unwrap(),
            "x"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_link_to_a_missing_file_is_judged_by_where_it_leads() {
        use std::os::unix::fs::symlink;
        let dir = scratch("dangling");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        symlink(root.join("new.txt"), root.join("inside")).unwrap();
        symlink(other.join("new.txt"), root.join("outside")).unwrap();
        let roots = json!([text(&root)]);

        assert!(strict_write(&text(&root.join("inside")), &roots).is_some());
        assert!(root.join("new.txt").is_file());
        assert!(strict_write(&text(&root.join("outside")), &roots).is_none());
        assert!(!other.join("new.txt").exists());
        let written = write(&text(&root.join("outside")), &roots).unwrap();
        assert!(warning(&written).is_some());
        assert!(other.join("new.txt").is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_root_given_through_a_link_still_holds_its_files() {
        // As on macOS, where `/tmp` and `/var` are links into `/private`.
        let dir = scratch("root-link");
        let real = dir.join("real");
        std::fs::create_dir_all(&real).unwrap();
        std::fs::write(real.join("a.txt"), b"a").unwrap();
        std::os::unix::fs::symlink(&real, dir.join("alias")).unwrap();
        let roots = json!([text(&dir.join("alias"))]);

        assert!(strict_read(&text(&dir.join("alias").join("a.txt")), &roots).is_some());
        assert!(strict_read(&text(&real.join("a.txt")), &roots).is_some());
        assert!(strict_write(&text(&dir.join("alias").join("b.txt")), &roots).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_device_file_is_used_with_a_warning_unless_strict() {
        // A serial device, /dev/urandom: read and written by
        // default, bounded by max_bytes, saying what it is; refused in strict.
        let warned = |result: serde_json::Value| {
            let warning = result["warning"].as_str().unwrap_or_default().to_string();
            assert!(warning.contains("a character device"), "{warning}");
            assert!(
                warning.contains("RUNNER_STRICT_PATH_CONFINEMENT is off"),
                "{warning}"
            );
            result
        };
        for roots in [json!(null), json!(["/dev"])] {
            let zeros = done(read_file(&json!({
                "path": "/dev/zero", "max_bytes": 8, "allowed_roots": roots,
            })))
            .unwrap();
            assert_eq!(warned(zeros)["content"], "\0".repeat(8));
            let empty = warned(read("/dev/null", &roots).unwrap());
            assert_eq!(empty["content"], "");
            warned(write("/dev/null", &roots).unwrap());
            for device in ["/dev/null", "/dev/zero"] {
                let strict = command(device, &roots, true);
                for refusal in [read_file(&strict).error, write_file(&strict).error] {
                    let refusal = refusal.unwrap();
                    assert!(
                        refusal.contains("RUNNER_STRICT_PATH_CONFINEMENT is on"),
                        "{refusal}"
                    );
                }
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_device_handle_is_judged_by_type() {
        // The handle is a real device (opening /dev/null succeeds); the fstat on
        // it, not the name, is what tells - this is the device swap the pre-open
        // check cannot catch: a warning by default, refused in strict.
        let dev = std::fs::File::open("/dev/null").unwrap();
        let confined = |strict| Confinement {
            roots: vec![PathBuf::from("/dev")],
            strict,
        };
        let warning = verify_opened(&dev, "/dev/null", &confined(false), Opened::File)
            .unwrap()
            .unwrap();
        assert!(warning.contains("a character device"), "{warning}");
        assert!(verify_opened(&dev, "/dev/null", &confined(true), Opened::File).is_err());
        // Never a folder where a file was expected, or the reverse, in any mode.
        let dir = std::fs::File::open("/dev").unwrap();
        for strict in [false, true] {
            assert!(verify_opened(&dir, "/dev", &confined(strict), Opened::File).is_err());
            assert!(verify_opened(&dev, "/dev/null", &confined(strict), Opened::Dir).is_err());
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_socket_or_a_fifo_never_holds_the_runner() {
        // Reaching the assertions proves no open blocked on the FIFO.
        let dir = scratch("special");
        let socket = dir.join("sock");
        let _listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        let fifo = dir.join("fifo");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        // SAFETY: `c` is a valid NUL-terminated path that outlives the call.
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
        for roots in [json!(null), json!([text(&dir)])] {
            // A FIFO with no writer reads as empty, with the warning, instead of
            // waiting forever; a write with no reader fails instead of waiting.
            let read_back = read(&text(&fifo), &roots).unwrap();
            assert_eq!(read_back["content"], "");
            let warning = read_back["warning"].as_str().unwrap();
            assert!(warning.contains("a named pipe (FIFO)"), "{warning}");
            assert!(write(&text(&fifo), &roots).is_none());
            // A socket cannot be opened as a file, as before.
            assert!(read(&text(&socket), &roots).is_none());
            assert!(write(&text(&socket), &roots).is_none());
            for path in [&fifo, &socket] {
                let strict = command(&text(path), &roots, true);
                for refusal in [read_file(&strict).error, write_file(&strict).error] {
                    let refusal = refusal.unwrap();
                    assert!(
                        refusal.contains("RUNNER_STRICT_PATH_CONFINEMENT is on"),
                        "{refusal}"
                    );
                }
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn an_unconfined_open_judges_the_handle_it_got() {
        // What an unconfined read or write opens is judged from its handle, so
        // a file swapped for a FIFO or a device after the first check neither
        // hangs the open nor escapes strict mode.
        let dir = scratch("unconfined-swap");
        let fifo = dir.join("pipe");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        // SAFETY: `c` is a valid NUL-terminated path that outlives the call.
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
        let (_, warning) = open_unconfined(&text(&fifo), false, false).unwrap();
        assert!(warning.unwrap().contains("a named pipe (FIFO)"));
        assert!(open_unconfined(&text(&fifo), false, true).is_err());
        let (_, warning) = open_unconfined("/dev/null", true, false).unwrap();
        assert!(warning.unwrap().contains("a character device"));
        assert!(open_unconfined("/dev/null", true, true).is_err());
        assert!(open_unconfined(&text(&dir), false, false).is_err());
        // A regular file: no warning, and a write truncates it first.
        let file = dir.join("a.txt");
        std::fs::write(&file, b"longer").unwrap();
        let (mut handle, warning) = open_unconfined(&text(&file), true, true).unwrap();
        assert!(warning.is_none());
        std::io::Write::write_all(&mut handle, b"x").unwrap();
        drop(handle);
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "x");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_fifo_with_a_writer_is_read_as_before() {
        let dir = scratch("fifo-writer");
        let fifo = dir.join("pipe");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        // SAFETY: `c` is a valid NUL-terminated path that outlives the call.
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
        for roots in [json!(null), json!([text(&dir)])] {
            let path = fifo.clone();
            // The writer blocks on its open until the read opens the other end,
            // then writes and closes: the read gets everything, then EOF.
            let writer = std::thread::spawn(move || {
                use std::io::Write;
                let mut end = OpenOptions::new().write(true).open(&path).unwrap();
                end.write_all(b"streamed").unwrap();
            });
            // Give the writer time to be waiting on its open.
            std::thread::sleep(std::time::Duration::from_millis(500));
            let read_back = read(&text(&fifo), &roots).unwrap();
            writer.join().unwrap();
            assert_eq!(read_back["content"], "streamed");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn opening_a_fifo_returns_instead_of_hanging() {
        // A FIFO swapped onto a checked path must not hang the open (O_NONBLOCK);
        // the handle's type then says what it is: a warning by default, refused
        // in strict, never listed as a folder. Reaching the asserts at all
        // proves the open did not block.
        let dir = scratch("fifo");
        let root = dir.join("root");
        std::fs::create_dir_all(&root).unwrap();
        let fifo = root.join("pipe");
        let c = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        // SAFETY: `c` is a valid NUL-terminated path that outlives the call.
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);

        for strict in [false, true] {
            let confinement = confined_to(&root, strict);
            let read_handle = open_confined_read(&text(&fifo)).unwrap();
            let judged = verify_opened(&read_handle, &text(&fifo), &confinement, Opened::File);
            if strict {
                assert!(judged.is_err());
            } else {
                assert!(judged.unwrap().unwrap().contains("a named pipe (FIFO)"));
            }
            let dir_handle = open_dir(&text(&fifo)).unwrap();
            assert!(verify_opened(&dir_handle, &text(&fifo), &confinement, Opened::Dir).is_err());
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(not(windows))]
    #[test]
    fn windows_device_names_are_ordinary_files_elsewhere() {
        let dir = scratch("device-names");
        let roots = json!([text(&dir)]);
        for name in ["aux.c", "con.txt", "nul", "prn.log", "COM1.md"] {
            let file = text(&dir.join(name));
            assert!(write(&file, &json!(null)).is_some(), "{name}");
            assert!(read(&file, &json!(null)).is_some(), "{name}");
            assert!(write(&file, &roots).is_some(), "{name}");
            assert!(read(&file, &roots).is_some(), "{name}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A directory junction: the link any Windows user may create. `false`,
    /// and the calling test is skipped, where the file system has none.
    #[cfg(windows)]
    fn junction(link: &Path, target: &Path) -> bool {
        let made = std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(link)
            .arg(target)
            .output()
            .is_ok_and(|out| out.status.success());
        if !made {
            eprintln!("skipped: no junction could be made at {}", link.display());
        }
        made
    }

    #[cfg(windows)]
    #[test]
    fn a_windows_junction_is_judged_by_where_it_leads() {
        let dir = scratch("junctions");
        let root = dir.join("root");
        let second = dir.join("second");
        let other = dir.join("other");
        for folder in [&root, &second, &other] {
            std::fs::create_dir_all(folder).unwrap();
        }
        std::fs::write(second.join("b.txt"), b"b").unwrap();
        std::fs::write(other.join("c.txt"), b"c").unwrap();
        let links = [
            (root.join("to-second"), &second),
            (root.join("to-other"), &other),
            (dir.join("alias"), &root),
        ];
        if !links.iter().all(|(link, target)| junction(link, target)) {
            let _ = std::fs::remove_dir_all(&dir);
            return;
        }
        let roots = json!([text(&root), text(&second)]);

        assert!(strict_read(&text(&root.join("to-second").join("b.txt")), &roots).is_some());
        assert!(strict_write(&text(&root.join("to-second").join("d.txt")), &roots).is_some());
        assert!(strict_read(&text(&root.join("to-other").join("c.txt")), &roots).is_none());
        assert!(strict_list(&text(&root.join("to-other")), &roots).is_none());
        assert!(strict_write(&text(&root.join("to-other").join("e.txt")), &roots).is_none());
        assert!(!other.join("e.txt").exists());
        // The default follows the junction, as before, and says so.
        let read_back = read(&text(&root.join("to-other").join("c.txt")), &roots).unwrap();
        assert_eq!(read_back["content"], "c");
        assert!(warning(&read_back).is_some());
        // A root given through a junction still holds its files.
        let aliased = json!([text(&dir.join("alias"))]);
        assert!(strict_write(&text(&root.join("f.txt")), &aliased).is_some());
        assert!(strict_read(&text(&dir.join("alias").join("f.txt")), &aliased).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_listed_junction_is_reported_as_the_link() {
        let dir = scratch("list-junction");
        let root = dir.join("root");
        let other = dir.join("other");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        std::fs::write(other.join("c.txt"), b"c").unwrap();
        if !junction(&root.join("to-other"), &other) {
            let _ = std::fs::remove_dir_all(&dir);
            return;
        }
        let listed = list(&text(&root), &json!([text(&root)])).unwrap();
        let link = listed["entries"]
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["name"] == "to-other")
            .unwrap()
            .clone();
        assert_eq!(link["is_link"], true);
        assert_eq!(link["is_dir"], false);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Make `folder` case sensitive, as WSL creates its folders. `false`, and
    /// the calling test is skipped, where Windows refuses it.
    #[cfg(windows)]
    fn make_case_sensitive(folder: &Path) -> bool {
        let made = std::process::Command::new("fsutil")
            .args(["file", "setCaseSensitiveInfo"])
            .arg(folder)
            .arg("enable")
            .output()
            .is_ok_and(|out| out.status.success());
        if !made {
            eprintln!(
                "skipped: {} cannot be made case sensitive",
                folder.display()
            );
        }
        made
    }

    #[cfg(windows)]
    #[test]
    fn a_case_sensitive_folder_keeps_names_that_differ_in_case_apart() {
        let dir = scratch("case-sensitive");
        let work = dir.join("work");
        std::fs::create_dir_all(&work).unwrap();
        if !make_case_sensitive(&work) {
            let _ = std::fs::remove_dir_all(&dir);
            return;
        }
        let (root, sibling) = (work.join("Safe"), work.join("safe"));
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&sibling).unwrap();
        std::fs::write(root.join("a.txt"), b"a").unwrap();
        std::fs::write(sibling.join("secret.txt"), b"s").unwrap();
        let roots = json!([text(&root)]);

        let read_back = strict_read(&text(&root.join("a.txt")), &roots).unwrap();
        assert_eq!(warning(&read_back), None);
        assert!(strict_read(&text(&sibling.join("secret.txt")), &roots).is_none());
        assert!(strict_list(&text(&sibling), &roots).is_none());
        assert!(strict_write(&text(&sibling.join("b.txt")), &roots).is_none());
        // A folder that does not exist yet is another folder there too.
        assert!(strict_write(&text(&work.join("SAFE").join("c.txt")), &roots).is_none());
        assert!(!work.join("SAFE").exists());
        // The default runs it, as before, and says it is outside.
        let read_back = read(&text(&sibling.join("secret.txt")), &roots).unwrap();
        assert!(warning(&read_back).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_bare_drive_is_the_whole_drive() {
        // As the backend's check reads `D:`; Windows alone would take it for
        // that drive's current folder.
        let dir = scratch("bare-drive");
        let file = text(&dir.join("a.txt"));
        std::fs::write(&file, b"a").unwrap();
        let drive = file[..2].to_string();
        for root in [drive.to_uppercase(), drive.to_lowercase()] {
            let read_back = strict_read(&file, &json!([root])).unwrap();
            assert_eq!(read_back["content"], "a", "{root}");
            assert_eq!(warning(&read_back), None, "{root}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn the_unresolvable_volume_fallback_never_trusts_a_name_of_dots_and_spaces() {
        // Taken only where `canonicalize` fails (RAM disks, VirtualBox shared
        // folders); there the path as written is all the check has.
        let dir = scratch("dot-space");
        let allowed = dir.join("allowed");
        std::fs::create_dir_all(&allowed).unwrap();
        assert!(leads_where_it_says(&allowed.join("a.txt")));
        assert!(leads_where_it_says(&allowed.join(".config").join("a.txt")));
        for odd in [".. ", "..  ", ".. .", "... ", ". ", "..."] {
            assert!(
                !leads_where_it_says(&allowed.join(odd).join("a.txt")),
                "{odd:?}"
            );
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_windows_root_matches_in_every_spelling_the_backend_accepts() {
        let dir = scratch("win-spelling");
        let root = dir.join("Root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("a.txt"), b"a").unwrap();
        // Forward slashes, a trailing separator and another letter case.
        let roots = json!([format!(
            "{}/",
            text(&root).replace('\\', "/").to_uppercase()
        )]);

        let file = text(&root.join("a.txt")).to_lowercase();
        assert_eq!(strict_read(&file, &roots).unwrap()["path"], file);
        assert!(strict_list(&text(&root).to_lowercase(), &roots).is_some());
        assert!(strict_write(&file, &roots).is_some());
        // A missing root, written in another case than the path.
        let reports = json!([text(&dir.join("Out").join("Reports"))]);
        let new_file = text(&dir.join("out").join("REPORTS").join("a.txt"));
        assert!(strict_write(&new_file, &reports).is_some());
        assert!(dir.join("Out").join("Reports").join("a.txt").is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn an_ntfs_stream_inside_a_root_is_written_and_read() {
        let dir = scratch("stream");
        std::fs::write(dir.join("a.txt"), b"a").unwrap();
        let roots = json!([text(&dir)]);
        let stream = format!("{}:note", text(&dir.join("a.txt")));
        assert!(strict_write(&stream, &roots).is_some());
        assert_eq!(strict_read(&stream, &roots).unwrap()["content"], "x");
        assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "a");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_verbatim_drive_path_is_a_file_not_a_device() {
        let dir = scratch("verbatim");
        let file = std::fs::canonicalize(&dir).unwrap().join("a.txt");
        assert!(text(&file).starts_with(r"\\?\"));
        std::fs::write(&file, b"a").unwrap();
        assert_eq!(read(&text(&file), &json!(null)).unwrap()["content"], "a");
        assert!(write(&text(&file), &json!(null)).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_drive_path_behind_a_namespace_prefix_is_a_file() {
        // \\.\C:\..., \??\C:\... and \\?\C:\... open a file on a drive, and
        // what Windows opens decides: a file, served as one with no warning,
        // strict or not. A full-control runner read such a path on the baseline.
        let dir = scratch("namespace-drive");
        let verbatim = std::fs::canonicalize(&dir).unwrap().join("a.txt");
        let tail = text(&verbatim)[r"\\?\".len()..].to_string();
        for prefix in [r"\\?\", r"\\.\", r"\??\"] {
            let p = format!("{prefix}{tail}");
            for strict in [false, true] {
                std::fs::write(&verbatim, b"a").unwrap();
                let served = done(read_file(&command(&p, &json!(null), strict)))
                    .unwrap_or_else(|| panic!("{p} strict={strict}"));
                assert_eq!(served["content"], "a", "{p} strict={strict}");
                assert_eq!(warning(&served), None, "{p} strict={strict}");
                let written = done(write_file(&command(&p, &json!(null), strict)))
                    .unwrap_or_else(|| panic!("{p} strict={strict}"));
                assert_eq!(warning(&written), None, "{p} strict={strict}");
                assert_eq!(std::fs::read_to_string(&verbatim).unwrap(), "x", "{p}");
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Write `name` under `dir` through a verbatim path, where Windows reads no
    /// name as a device, so the file exists whatever the release makes of it.
    #[cfg(windows)]
    fn file_under_any_name(dir: &Path, name: &str, content: &[u8]) -> PathBuf {
        let file = std::fs::canonicalize(dir).unwrap().join(name);
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        std::fs::write(&file, content).unwrap();
        file
    }

    #[cfg(windows)]
    #[test]
    fn a_name_windows_opens_as_a_file_is_read_and_written_as_one() {
        // Which reserved names open a device depends on the Windows release
        // (Windows 11 opens `nul.txt`, `con.log` or a `CON` inside a folder as
        // files, older releases as devices), and a reserved name in the middle
        // of a path is a folder on every release. A name Windows opens as a
        // file is read and written as a plain file, with no warning, in every
        // mode; a name it opens as a device is served with a warning (refused
        // under strict) - covered by a_windows_device_is_used_with_a_warning_unless_strict.
        let dir = scratch("reserved-names");
        for name in [
            "nul.txt",
            "NUL.tar.gz",
            "nul .txt",
            "con.log",
            "aux.c",
            "CON",
            "nul",
            r"aux\x.txt",
            r"con.d\f.txt",
            r"nul.d\n.txt",
        ] {
            let plain = text(&dir.join(name));
            for (roots, strict) in [
                (json!(null), false),
                (json!([text(&dir)]), false),
                (json!([text(&dir)]), true),
            ] {
                let file = file_under_any_name(&dir, name, b"file");
                // Only the names this release opens as a real file; the rest are
                // devices, covered by the device warn/strict test.
                if !std::fs::metadata(&plain).is_ok_and(|meta| meta.is_file() && meta.len() == 4) {
                    continue;
                }
                let served = done(read_file(&command(&plain, &roots, strict))).unwrap();
                assert_eq!(served["content"], "file", "{name} {roots} {strict}");
                assert_eq!(warning(&served), None, "{name} {roots} {strict}");
                assert!(
                    done(write_file(&command(&plain, &roots, strict))).is_some(),
                    "{name} {roots} {strict}"
                );
                assert_eq!(std::fs::read_to_string(&file).unwrap(), "x", "{name}");
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_windows_device_is_used_with_a_warning_unless_strict() {
        let unconfined = json!(null);
        // Whatever the spelling, a device is what Windows opened for the path,
        // told by the handle's file type, never by the name.
        for device in ["NUL", r"C:\NUL", "NUL:", "nul ", r"\\.\NUL"] {
            // Written by default, saying what it is.
            let written = done(write_file(&command(device, &unconfined, false)))
                .unwrap_or_else(|| panic!("{device}"));
            let said = warning(&written).unwrap_or_default();
            assert!(said.contains("a Windows device"), "{device}: {said}");
            assert!(
                said.contains("RUNNER_STRICT_PATH_CONFINEMENT is off"),
                "{device}: {said}"
            );
            // Strict: refused once Windows opened it, before anything is written.
            let refusal = write_file(&command(device, &unconfined, true))
                .error
                .unwrap();
            assert!(
                refusal.contains("RUNNER_STRICT_PATH_CONFINEMENT is on"),
                "{device}: {refusal}"
            );
            // A read is never refused by the name: where Windows cannot describe
            // the device for the size check, it fails with the OS's own error,
            // as on the baseline; strict never serves it.
            let read_default = read_file(&command(device, &unconfined, false));
            if let Some(error) = read_default.error {
                assert!(!error.contains("RUNNER_STRICT"), "{device}: {error}");
            }
            assert!(read_file(&command(device, &unconfined, true))
                .error
                .is_some());
        }
    }

    #[cfg(windows)]
    #[test]
    fn a_windows_device_inside_an_allowed_folder_is_written_with_a_warning_unless_strict() {
        // A restricted runner wrote `<allowed folder>\nul` on the baseline: Windows
        // opens it as the null device, whose handle cannot describe itself
        // (`File::metadata` fails). The check of the opened handle must not take
        // that for a failure: written with the device warning by default,
        // refused under strict once opened, before anything is written.
        let dir = scratch("confined-device");
        let device = text(&dir.join("nul"));
        if !File::open(&device).is_ok_and(|handle| device_handle(&handle)) {
            eprintln!("skipped: this Windows release opens {device} as a file");
            let _ = std::fs::remove_dir_all(&dir);
            return;
        }
        let roots = json!([text(&dir)]);
        let outcome = write_file(&command(&device, &roots, false));
        let error = outcome.error.clone();
        let written = done(outcome).unwrap_or_else(|| panic!("{device}: {error:?}"));
        let said = warning(&written).unwrap();
        assert!(said.contains("a Windows device"), "{said}");
        let refusal = write_file(&command(&device, &roots, true)).error.unwrap();
        assert!(
            refusal.contains("RUNNER_STRICT_PATH_CONFINEMENT is on"),
            "{refusal}"
        );
        // A device is no folder, in any mode, as on the baseline.
        for strict in [false, true] {
            assert!(list_dir(&command(&device, &roots, strict)).error.is_some());
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_device_handle_is_judged_by_its_file_type_where_windows_cannot_describe_it() {
        // Windows cannot describe a device handle (`File::metadata` fails on
        // it); its file type says it is a device, whatever the path's name: a
        // warning by default, refused in strict, never a folder. The unconfined
        // open takes the same branch.
        let dir = scratch("device-handle");
        let nul = File::open("NUL").unwrap();
        let (default, strict) = (confined_to(&dir, false), confined_to(&dir, true));
        for name in ["NUL".to_string(), text(&dir.join("a.txt"))] {
            let said = verify_opened(&nul, &name, &default, Opened::File)
                .unwrap()
                .unwrap();
            assert!(said.contains("a Windows device"), "{name}: {said}");
            assert!(
                verify_opened(&nul, &name, &strict, Opened::File).is_err(),
                "{name}"
            );
            for confinement in [&default, &strict] {
                assert!(verify_opened(&nul, &name, confinement, Opened::Dir).is_err());
            }
        }
        let (_, added) = open_unconfined("NUL", true, false).unwrap();
        assert!(added.is_some_and(|said| said.contains("a Windows device")));
        assert!(open_unconfined("NUL", true, true).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn a_raw_volume_is_no_regular_file_although_its_handle_is_a_disk() {
        // A volume opened raw (`\\.\C:`) is a disk-type handle, not a character
        // device, and Windows answers no file query for it: no regular file,
        // so a warning by default and a refusal in strict, never a folder.
        // Opened with no access right, which needs no administrator.
        use std::os::windows::fs::OpenOptionsExt;
        let drive = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".to_string());
        let volume = format!(r"\\.\{drive}");
        let handle = OpenOptions::new().access_mode(0).open(&volume).unwrap();
        assert!(handle.metadata().is_err(), "{volume}");
        assert!(!device_handle(&handle), "{volume}");
        let said = undescribed_handle_kind(&handle, &volume, false)
            .unwrap()
            .unwrap();
        assert!(said.contains("a raw volume or disk"), "{said}");
        assert!(
            said.contains("RUNNER_STRICT_PATH_CONFINEMENT is off"),
            "{said}"
        );
        let refusal = undescribed_handle_kind(&handle, &volume, true)
            .unwrap_err()
            .to_string();
        assert!(
            refusal.contains("RUNNER_STRICT_PATH_CONFINEMENT is on"),
            "{refusal}"
        );
        // A confined command that opened it: the same rule, never a folder.
        let dir = scratch("raw-volume");
        let (default, strict) = (confined_to(&dir, false), confined_to(&dir, true));
        let said = verify_opened(&handle, &volume, &default, Opened::File)
            .unwrap()
            .unwrap();
        assert!(said.contains("a raw volume or disk"), "{said}");
        assert!(verify_opened(&handle, &volume, &strict, Opened::File).is_err());
        for confinement in [&default, &strict] {
            assert!(verify_opened(&handle, &volume, confinement, Opened::Dir).is_err());
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// `file` as a `\\?\GLOBALROOT\Device\<volume>\...` path, from the NT name
    /// its handle resolves to; `None` where the volume is not a numbered
    /// harddisk volume.
    #[cfg(windows)]
    fn globalroot_path(file: &Path) -> Option<String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{GetFinalPathNameByHandleW, VOLUME_NAME_NT};
        let handle = File::open(file).ok()?;
        let mut buf = vec![0u16; 2048];
        // SAFETY: `buf` holds `buf.len()` WCHARs.
        let written = unsafe {
            GetFinalPathNameByHandleW(
                handle.as_raw_handle() as _,
                buf.as_mut_ptr(),
                buf.len() as u32,
                VOLUME_NAME_NT,
            )
        };
        if written == 0 || written as usize >= buf.len() {
            return None;
        }
        let nt = String::from_utf16(&buf[..written as usize]).ok()?;
        nt.starts_with(r"\Device\HarddiskVolume")
            .then(|| format!(r"\\?\GLOBALROOT{nt}"))
    }

    #[cfg(windows)]
    #[test]
    fn a_file_reached_through_its_volume_device_is_a_file_strict_or_not() {
        // How a volume shadow copy is read: `\\?\GLOBALROOT\Device\
        // HarddiskVolumeShadowCopyN\...` reaches a file through a volume's NT
        // device, like `\\?\GLOBALROOT\Device\HarddiskVolumeN\...` here. What
        // Windows opens is a regular file, so it is read, listed and written as
        // one, with no warning, strict or not (full control: no allowed folders).
        let dir = scratch("globalroot");
        let file = dir.join("a.txt");
        std::fs::write(&file, b"a").unwrap();
        let Some(path) = globalroot_path(&file) else {
            eprintln!("skipped: the scratch folder is not on a numbered harddisk volume");
            let _ = std::fs::remove_dir_all(&dir);
            return;
        };
        let folder = path[..path.rfind('\\').unwrap()].to_string();
        let unconfined = json!(null);
        for strict in [false, true] {
            std::fs::write(&file, b"a").unwrap();
            let read_back = done(read_file(&command(&path, &unconfined, strict)))
                .unwrap_or_else(|| panic!("{path} strict={strict}"));
            assert_eq!(read_back["content"], "a");
            assert_eq!(warning(&read_back), None, "strict={strict}");
            let listed = done(list_dir(&command(&folder, &unconfined, strict)))
                .unwrap_or_else(|| panic!("{folder} strict={strict}"));
            assert!(listed["entries"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e["name"] == "a.txt"));
            assert_eq!(warning(&listed), None, "strict={strict}");
            let written = done(write_file(&command(&path, &unconfined, strict)))
                .unwrap_or_else(|| panic!("{path} strict={strict}"));
            assert_eq!(warning(&written), None, "strict={strict}");
            assert_eq!(std::fs::read_to_string(&file).unwrap(), "x");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn an_allowed_folder_named_like_a_device_confines_as_any_folder() {
        // An allowed folder is never dropped for its name: `aux` or `con.d`
        // are folders Windows opens as folders.
        let base = scratch("device-named-roots");
        for name in ["aux", "con.d", "nul.d"] {
            let root = base.join(name);
            std::fs::create_dir_all(&root).unwrap();
            let roots = json!([text(&root)]);
            let file = text(&root.join("a.txt"));
            assert!(strict_write(&file, &roots).is_some(), "{name}");
            assert_eq!(
                strict_read(&file, &roots).unwrap()["content"],
                "x",
                "{name}"
            );
            assert!(
                strict_read(&text(&base.join("b.txt")), &roots).is_none(),
                "{name}"
            );
        }
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(windows)]
    #[test]
    fn names_that_merely_start_like_a_device_are_files() {
        let dir = scratch("lookalikes");
        let roots = json!([text(&dir)]);
        for name in [
            "console.log",
            "CONTRIBUTING.md",
            "auxiliary.txt",
            "com10.txt",
            "lpt.md",
            "nullable.rs",
            r"conf\settings.toml",
        ] {
            let file = text(&dir.join(name));
            assert!(write(&file, &json!(null)).is_some(), "{name}");
            assert!(read(&file, &json!(null)).is_some(), "{name}");
            assert!(strict_read(&file, &roots).is_some(), "{name}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
