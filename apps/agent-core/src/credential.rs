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

//! Storage of an agent's durable credential.
//!
//! Preferred backend is OS-native: DPAPI (Windows), Keychain (macOS) or the
//! Secret Service / libsecret (Linux) via the `keyring` crate. The credential
//! is written once at enrollment and read on every start; it never lands in the
//! config file or a log line.
//!
//! Fallback: a headless Linux server usually runs no Secret Service at all, and
//! there the keyring call fails outright - which used to make `enroll` die on a
//! box where everything else worked. So when the OS keychain is
//! unavailable we fall back to a 0600 file in the agent's own config dir, and
//! say so loudly. That is strictly better than the alternatives available on
//! such a host (an env var in a systemd unit, or no unattended agent at all),
//! and it is the same protection the config file already relies on.

use std::path::PathBuf;

use anyhow::{Context, Result};
#[cfg(feature = "keychain")]
use tracing::warn;

/// The directory of the credential file.
#[derive(Clone)]
enum Dir {
    /// Resolved on use, never at construction: a keychain write must not
    /// depend on the config directory resolving.
    Resolve(fn() -> Result<PathBuf>),
    Fixed(PathBuf),
}

/// Where an agent keeps its credential: the keychain service it files it under
/// and the directory of the file.
#[derive(Clone)]
pub struct CredentialStore {
    #[cfg_attr(not(feature = "keychain"), allow(dead_code))]
    service: &'static str,
    dir: Dir,
    keychain: bool,
}

impl CredentialStore {
    /// A store under keychain `service`, falling back to files in the
    /// directory `config_dir` resolves (the runner, a per-user app).
    pub const fn new(service: &'static str, config_dir: fn() -> Result<PathBuf>) -> Self {
        Self {
            service,
            dir: Dir::Resolve(config_dir),
            keychain: true,
        }
    }

    /// A store of owner-only files in `dir`, with no keychain (the relay, a
    /// system service: the account that runs it is rarely the one that
    /// enrolled it, and only a file in its own state directory is seen by
    /// both).
    pub fn files_in(service: &'static str, dir: PathBuf) -> Self {
        Self {
            service,
            dir: Dir::Fixed(dir),
            keychain: false,
        }
    }

    fn config_dir(&self) -> Result<PathBuf> {
        match &self.dir {
            Dir::Resolve(resolve) => resolve(),
            Dir::Fixed(dir) => Ok(dir.clone()),
        }
    }

    /// Whether the OS keychain is consulted: asked for, and compiled in.
    fn uses_keychain(&self) -> bool {
        cfg!(feature = "keychain") && self.keychain
    }

    #[cfg(feature = "keychain")]
    fn entry(&self, id: &str) -> Result<keyring::Entry> {
        // Account = the agent's id so several agents enrolled on one machine
        // (rare, but supported) never collide in the keychain.
        keyring::Entry::new(self.service, id).context("opening OS keychain entry")
    }

    /// Where the fallback credential lives for an id.
    ///
    /// The id is sanitized rather than trusted: it reaches us from the server,
    /// and it is about to become a path segment.
    pub fn fallback_path(&self, id: &str) -> Result<PathBuf> {
        let safe: String = id
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        Ok(self.config_dir()?.join(format!("credential-{safe}")))
    }

    fn store_fallback(&self, id: &str, token: &str) -> Result<()> {
        let path = self.fallback_path(id)?;
        let dir = path
            .parent()
            .context("resolving the credential directory")?
            .to_path_buf();
        std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
        #[cfg(unix)]
        {
            use std::io::Write;
            use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

            // Owner-only on the directory too: the file name embeds the id, and
            // a directory another local user can write to is one where they can
            // swap our file for a symlink. Failing here is the point - a
            // credential does not get written into a directory we could not
            // secure.
            std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))
                .with_context(|| format!("restricting {} to owner-only", dir.display()))?;

            // Remove-then-create-exclusively, at 0600 from the open() itself:
            //   * O_CREAT|O_EXCL (`create_new`) never follows a symlink, so an
            //     entry planted between the two calls makes this FAIL rather
            //     than write the credential wherever it points;
            //   * the mode is set at creation, never widened-then-narrowed.
            //     Writing first and chmod-ing after would leave the secret at
            //     0644 (the usual umask) for a window - and permanently so if
            //     the process died in it, because the next start reads the file
            //     back happily and never rewrites it;
            //   * recreating also drops a wider-moded file left by an older
            //     build.
            // This fallback exists for shared Linux servers, i.e. exactly the
            // hosts that have other local users on them.
            match std::fs::remove_file(&path) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => {
                    return Err(
                        anyhow::Error::new(err).context(format!("replacing {}", path.display()))
                    )
                }
            }
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)
                .with_context(|| format!("writing {}", path.display()))?;
            f.write_all(token.as_bytes())
                .with_context(|| format!("writing {}", path.display()))?;
        }
        #[cfg(not(unix))]
        {
            // Windows: the file inherits the per-user ACL of the profile
            // directory (or, for a service, of its own data directory).
            std::fs::write(&path, token).with_context(|| format!("writing {}", path.display()))?;
        }
        Ok(())
    }

    fn load_fallback(&self, id: &str) -> Result<Option<String>> {
        let path = self.fallback_path(id)?;
        // Never read THROUGH a symlink: whatever it points at is not a
        // credential we wrote, and treating an attacker-chosen file as our
        // token is worse than having none. symlink_metadata does not follow, so
        // this sees the link.
        match std::fs::symlink_metadata(&path) {
            Ok(meta) if !meta.is_file() => {
                return Err(anyhow::anyhow!(
                    "{} is not a regular file; refusing to read a credential from it",
                    path.display()
                ))
            }
            _ => {}
        }
        match std::fs::read_to_string(&path) {
            Ok(token) => Ok(Some(token.trim().to_string()).filter(|t| !t.is_empty())),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(err) => Err(anyhow::Error::new(err)
                .context(format!("reading credential file {}", path.display()))),
        }
    }

    /// Persist the credential for an id.
    pub fn store(&self, id: &str, token: &str) -> Result<()> {
        if !self.uses_keychain() {
            return self.store_fallback(id, token);
        }
        self.store_keychain(id, token)
    }

    #[cfg(not(feature = "keychain"))]
    fn store_keychain(&self, id: &str, token: &str) -> Result<()> {
        self.store_fallback(id, token)
    }

    #[cfg(feature = "keychain")]
    fn store_keychain(&self, id: &str, token: &str) -> Result<()> {
        let attempt = self.entry(id).and_then(|e| {
            e.set_password(token)
                .context("writing credential to the OS keychain")
        });
        match attempt {
            Ok(()) => Ok(()),
            Err(err) => {
                let path = self.fallback_path(id)?;
                warn!(
                    "no usable OS keychain ({err:#}); storing the credential in {} \
                     with owner-only permissions instead",
                    path.display()
                );
                self.store_fallback(id, token)
            }
        }
    }

    /// Load the credential for an id, or `None` when absent.
    ///
    /// Both backends are consulted: an agent enrolled before a keychain
    /// appeared (or after one disappeared) must keep starting.
    pub fn load(&self, id: &str) -> Result<Option<String>> {
        if !self.uses_keychain() {
            return self.load_fallback(id);
        }
        self.load_keychain(id)
    }

    #[cfg(not(feature = "keychain"))]
    fn load_keychain(&self, id: &str) -> Result<Option<String>> {
        self.load_fallback(id)
    }

    #[cfg(feature = "keychain")]
    fn load_keychain(&self, id: &str) -> Result<Option<String>> {
        match self.entry(id).map(|e| e.get_password()) {
            Ok(Ok(token)) => return Ok(Some(token)),
            Ok(Err(keyring::Error::NoEntry)) => {}
            Ok(Err(err)) => {
                warn!("could not read the OS keychain ({err}); trying the credential file");
            }
            Err(err) => {
                warn!("no usable OS keychain ({err:#}); trying the credential file");
            }
        }
        self.load_fallback(id)
    }

    /// Delete the stored credential (used by `reset`). Missing is not an error.
    ///
    /// Clears BOTH backends: leaving a stale file behind would silently
    /// re-enroll the machine on the next start.
    pub fn delete(&self, id: &str) -> Result<()> {
        #[cfg(feature = "keychain")]
        if self.uses_keychain() {
            if let Ok(e) = self.entry(id) {
                match e.delete_credential() {
                    Ok(()) | Err(keyring::Error::NoEntry) => {}
                    Err(err) => warn!("could not delete the keychain credential: {err}"),
                }
            }
        }
        let path = self.fallback_path(id)?;
        match std::fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(err) => Err(anyhow::Error::new(err)
                .context(format!("removing credential file {}", path.display()))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::CredentialStore;
    use std::path::PathBuf;

    fn dir() -> anyhow::Result<PathBuf> {
        Ok(std::env::temp_dir().join("agent-core-credential-tests"))
    }

    const STORE: CredentialStore = CredentialStore::new("agent-core-tests", dir);

    #[test]
    fn fallback_path_stays_inside_the_config_dir() {
        let path = STORE.fallback_path("rly-abc123").unwrap();
        assert_eq!(
            path.file_name().unwrap().to_str().unwrap(),
            "credential-rly-abc123"
        );
        assert_eq!(path.parent().unwrap(), dir().unwrap());
    }

    #[test]
    fn fallback_path_neutralizes_a_hostile_id() {
        // The id arrives from the server and becomes a path segment. Anything
        // outside [A-Za-z0-9_-] collapses to '_', so separators and dots (and
        // therefore "..") cannot survive into the file name.
        let path = STORE.fallback_path("../../etc/cron.d/pwn").unwrap();
        let name = path.file_name().unwrap().to_str().unwrap();
        assert_eq!(name, "credential-______etc_cron_d_pwn");
        assert!(!name.contains('/') && !name.contains(".."));
        assert_eq!(path.parent().unwrap(), dir().unwrap());
    }

    #[test]
    fn a_file_store_round_trips_without_any_keychain() {
        let dir = std::env::temp_dir().join(format!("agent-core-files-{}", std::process::id()));
        let store = CredentialStore::files_in("agent-core-tests", dir.clone());
        assert_eq!(store.load("rly-1").unwrap(), None);
        store.store("rly-1", "rly-secret\n").unwrap();
        assert_eq!(store.load("rly-1").unwrap().as_deref(), Some("rly-secret"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(store.fallback_path("rly-1").unwrap())
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        store.delete("rly-1").unwrap();
        assert_eq!(store.load("rly-1").unwrap(), None);
        store.delete("rly-1").unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unresolvable_directory_is_an_error_not_a_panic() {
        fn none() -> anyhow::Result<PathBuf> {
            Err(anyhow::anyhow!("no home directory"))
        }
        let store = CredentialStore::new("agent-core-tests", none);
        assert!(store.fallback_path("x").is_err());
    }
}
