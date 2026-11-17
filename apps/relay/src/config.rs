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

//! The relay's configuration: a few parameters in one TOML file.
//!
//! ```toml
//! server_url = "https://xtm-one.example.com"
//! name = "paris-dc-1"
//! allowed_destinations = ["splunk.corp.local:8089", "10.20.0.0/16:443"]
//! ```
//!
//! Every key can be overridden by an `XTM_RELAY_<KEY>` environment variable
//! (lists comma- or space-separated), which is how a container is configured.
//! The file lives with the relay's state - its enrollment (`state.json`), its
//! credential and its last status - in one owner-only directory:
//!
//! * Linux: `/etc/xtm-one-relay/relay.toml`
//! * macOS: `/Library/Application Support/XTM One Relay/relay.toml`
//! * Windows: `%ProgramData%\XTM One Relay\relay.toml`
//!
//! `--config` / `XTM_RELAY_CONFIG` points elsewhere (the directory follows).

use std::net::SocketAddr;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::policy::Policy;

/// The default configuration file of this OS.
pub fn default_config_path() -> PathBuf {
    #[cfg(windows)]
    {
        let base = std::env::var_os("ProgramData")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
        base.join("XTM One Relay").join("relay.toml")
    }
    #[cfg(target_os = "macos")]
    {
        PathBuf::from("/Library/Application Support/XTM One Relay/relay.toml")
    }
    #[cfg(all(not(windows), not(target_os = "macos")))]
    {
        PathBuf::from("/etc/xtm-one-relay/relay.toml")
    }
}

/// How the relay reaches the platform.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TransportMode {
    /// The WebSocket, falling back to long-poll when it cannot be opened.
    Auto,
    /// The WebSocket only.
    Websocket,
    /// HTTP long-poll only (behind a proxy that refuses WebSocket upgrades).
    LongPoll,
}

impl TransportMode {
    fn parse(text: &str) -> Result<Self> {
        match text.trim().to_ascii_lowercase().replace('-', "_").as_str() {
            "auto" | "" => Ok(Self::Auto),
            "websocket" | "ws" => Ok(Self::Websocket),
            "long_poll" | "longpoll" | "poll" => Ok(Self::LongPoll),
            other => bail!("transport must be auto, websocket or long_poll (got '{other}')"),
        }
    }
}

/// relay.toml as written.
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    server_url: Option<String>,
    name: Option<String>,
    allowed_destinations: Option<Vec<String>>,
    insecure_tls_destinations: Option<Vec<String>>,
    ca_bundle: Option<PathBuf>,
    upstream_proxy: Option<String>,
    max_concurrent_requests: Option<u32>,
    log_level: Option<String>,
    health_listen: Option<String>,
    transport: Option<String>,
    auto_update: Option<bool>,
}

/// The effective configuration.
#[derive(Debug, Clone)]
pub struct Config {
    /// The configuration file (it may not exist yet).
    pub path: PathBuf,
    /// The state directory (the file's directory).
    pub dir: PathBuf,
    pub server_url: Option<String>,
    pub name: Option<String>,
    /// The local allow-list, the hard boundary of what the relay reaches.
    pub allowed_destinations: Vec<String>,
    /// Destinations whose TLS certificate is NOT verified (self-signed
    /// appliances); explicit, local, and said in the logs at every start.
    pub insecure_tls_destinations: Vec<String>,
    /// Extra CA certificates (PEM) trusted for destinations and the platform.
    pub ca_bundle: Option<PathBuf>,
    /// An HTTP(S) proxy for the connection to the platform (never for
    /// destinations). Behind one the relay uses long-poll.
    pub upstream_proxy: Option<String>,
    pub max_concurrent_requests: u32,
    pub log_level: String,
    /// A local health endpoint (`GET /healthz`), off unless set.
    pub health_listen: Option<SocketAddr>,
    pub transport: TransportMode,
    /// Apply the signed builds the platform offers (off in containers, which
    /// are updated by pulling the image).
    pub auto_update: bool,
}

/// Default and bounds of `max_concurrent_requests` (the platform's too).
pub const DEFAULT_MAX_CONCURRENT: u32 = 32;
const MAX_CONCURRENT_LIMIT: u32 = 256;

/// Whether the relay runs in the published container image.
pub fn in_container() -> bool {
    std::env::var("XTM_RELAY_CONTAINER")
        .map(|v| matches!(v.trim(), "1" | "true" | "yes"))
        .unwrap_or(false)
}

fn env(name: &str) -> Option<String> {
    std::env::var(format!("XTM_RELAY_{name}"))
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// A list as an environment variable spells it: comma- or space-separated.
pub fn split_list(text: &str) -> Vec<String> {
    text.split(|c: char| c == ',' || c.is_whitespace())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

fn parse_bool(name: &str, text: &str) -> Result<bool> {
    match text.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        other => bail!("{name} must be true or false (got '{other}')"),
    }
}

impl Config {
    /// Read `path` (a missing file is an empty one) and apply the environment.
    pub fn load(path: &Path) -> Result<Self> {
        let file = match std::fs::read_to_string(path) {
            Ok(text) => toml::from_str::<FileConfig>(&text)
                .with_context(|| format!("reading {}", path.display()))?,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => FileConfig::default(),
            Err(err) => {
                return Err(anyhow::Error::new(err).context(format!("reading {}", path.display())))
            }
        };
        Self::from_file(path, file)
    }

    fn from_file(path: &Path, mut file: FileConfig) -> Result<Self> {
        if let Some(v) = env("SERVER_URL") {
            file.server_url = Some(v);
        }
        if let Some(v) = env("NAME") {
            file.name = Some(v);
        }
        if let Some(v) = env("ALLOWED_DESTINATIONS") {
            file.allowed_destinations = Some(split_list(&v));
        }
        if let Some(v) = env("INSECURE_TLS_DESTINATIONS") {
            file.insecure_tls_destinations = Some(split_list(&v));
        }
        if let Some(v) = env("CA_BUNDLE") {
            file.ca_bundle = Some(PathBuf::from(v));
        }
        if let Some(v) = env("UPSTREAM_PROXY") {
            file.upstream_proxy = Some(v);
        }
        if let Some(v) = env("MAX_CONCURRENT_REQUESTS") {
            file.max_concurrent_requests = Some(
                v.parse()
                    .with_context(|| format!("XTM_RELAY_MAX_CONCURRENT_REQUESTS '{v}'"))?,
            );
        }
        if let Some(v) = env("LOG_LEVEL") {
            file.log_level = Some(v);
        }
        if let Some(v) = env("HEALTH_LISTEN") {
            file.health_listen = Some(v);
        }
        if let Some(v) = env("TRANSPORT") {
            file.transport = Some(v);
        }
        if let Some(v) = env("AUTO_UPDATE") {
            file.auto_update = Some(parse_bool("XTM_RELAY_AUTO_UPDATE", &v)?);
        }

        let max = file
            .max_concurrent_requests
            .unwrap_or(DEFAULT_MAX_CONCURRENT);
        if !(1..=MAX_CONCURRENT_LIMIT).contains(&max) {
            bail!("max_concurrent_requests must be 1-{MAX_CONCURRENT_LIMIT} (got {max})");
        }
        let health_listen = match file.health_listen.as_deref() {
            None | Some("") => None,
            Some(text) => Some(
                text.parse::<SocketAddr>()
                    .with_context(|| format!("health_listen '{text}' is not an address:port"))?,
            ),
        };
        let server_url = file
            .server_url
            .map(|u| u.trim().trim_end_matches('/').to_string())
            .filter(|u| !u.is_empty());
        let dir = path
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        Ok(Self {
            path: path.to_path_buf(),
            dir,
            server_url,
            name: file.name.filter(|n| !n.trim().is_empty()),
            allowed_destinations: file.allowed_destinations.unwrap_or_default(),
            insecure_tls_destinations: file.insecure_tls_destinations.unwrap_or_default(),
            ca_bundle: file.ca_bundle,
            upstream_proxy: file.upstream_proxy.filter(|p| !p.trim().is_empty()),
            max_concurrent_requests: max,
            log_level: file.log_level.unwrap_or_else(|| "info".to_string()),
            health_listen,
            transport: TransportMode::parse(file.transport.as_deref().unwrap_or("auto"))?,
            auto_update: file.auto_update.unwrap_or(!in_container()),
        })
    }

    /// The allow-list, parsed (the error names the first refused pattern).
    pub fn policy(&self) -> Result<Policy> {
        Policy::parse(&self.allowed_destinations).context("allowed_destinations")
    }

    /// The destinations whose certificate is not verified, parsed.
    pub fn insecure_tls_policy(&self) -> Result<Policy> {
        Policy::parse(&self.insecure_tls_destinations).context("insecure_tls_destinations")
    }

    /// The platform's base URL, refused unless it can carry a credential
    /// safely: `https://`, or `http://` to a loopback host (development).
    pub fn server_url(&self) -> Result<String> {
        let Some(url) = self.server_url.clone() else {
            bail!(
                "no server_url: set it in {} or XTM_RELAY_SERVER_URL (or run `xtm-one-relay enroll`)",
                self.path.display()
            );
        };
        require_secure_server_url(&url)?;
        Ok(url)
    }

    /// Everything a start would refuse, and what it would only warn about.
    pub fn check(&self) -> Result<Vec<String>> {
        let mut warnings = Vec::new();
        let policy = self.policy()?;
        self.insecure_tls_policy()?;
        if policy.is_empty() {
            warnings.push(
                "allowed_destinations is empty: every request will be refused until it names \
                 the destinations this relay may reach"
                    .to_string(),
            );
        }
        if policy.allows_all() {
            warnings.push(
                "allowed_destinations contains '*': this relay reaches any host on its network \
                 (except itself, link-local and cloud metadata addresses)"
                    .to_string(),
            );
        }
        if !self.insecure_tls_destinations.is_empty() {
            warnings.push(format!(
                "TLS certificates are NOT verified for: {}",
                self.insecure_tls_destinations.join(", ")
            ));
        }
        if let Some(bundle) = &self.ca_bundle {
            crate::tls::read_ca_bundle(bundle)?;
        }
        if let Some(proxy) = &self.upstream_proxy {
            reqwest::Proxy::all(proxy).with_context(|| format!("upstream_proxy '{proxy}'"))?;
        }
        if let Some(addr) = self.health_listen {
            if !addr.ip().is_loopback() {
                warnings.push(format!(
                    "health_listen {addr} is reachable from the network (it says whether the \
                     relay is connected, nothing more)"
                ));
            }
        }
        if let Some(url) = &self.server_url {
            require_secure_server_url(url)?;
        }
        Ok(warnings)
    }

    pub fn state_path(&self) -> PathBuf {
        self.dir.join("state.json")
    }

    pub fn status_path(&self) -> PathBuf {
        self.dir.join("status.json")
    }
}

/// Refuse to send a credential over an unencrypted transport: `https://`, or
/// `http://` to a loopback host only (local development).
pub fn require_secure_server_url(server_url: &str) -> Result<()> {
    let url = url::Url::parse(server_url)
        .with_context(|| format!("invalid server URL '{server_url}'"))?;
    match url.scheme() {
        "https" => Ok(()),
        "http" => {
            let loopback = match url.host() {
                Some(url::Host::Domain(d)) => d.eq_ignore_ascii_case("localhost"),
                Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
                Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
                None => false,
            };
            if loopback {
                Ok(())
            } else {
                bail!(
                    "refusing plain http:// to '{server_url}': the relay credential must travel \
                     over https:// (http:// is accepted for a loopback host only)"
                )
            }
        }
        other => bail!("unsupported server URL scheme '{other}' (use https://)"),
    }
}

/// What enrollment records: who this relay is and where it is enrolled.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct State {
    pub relay_id: String,
    pub server_url: String,
    pub group_id: Option<String>,
    pub group_name: Option<String>,
    pub enrolled_at: String,
}

impl State {
    pub fn load(config: &Config) -> Result<Option<Self>> {
        let path = config.state_path();
        match std::fs::read_to_string(&path) {
            Ok(text) => Ok(Some(
                serde_json::from_str(&text)
                    .with_context(|| format!("reading {}", path.display()))?,
            )),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(err) => Err(anyhow::Error::new(err).context(format!("reading {}", path.display()))),
        }
    }

    pub fn save(&self, config: &Config) -> Result<()> {
        prepare_dir(&config.dir)?;
        let path = config.state_path();
        write_private(&path, serde_json::to_string_pretty(self)?.as_bytes())
    }

    pub fn delete(config: &Config) -> Result<()> {
        match std::fs::remove_file(config.state_path()) {
            Ok(()) => Ok(()),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(err) => Err(err.into()),
        }
    }
}

/// Create the state directory and keep it to its owner (the credential lives
/// there). On Windows the directory under `%ProgramData%` inherits read access
/// for every local user, so the inheritance is cut and only SYSTEM and the
/// Administrators keep access.
pub fn prepare_dir(dir: &Path) -> Result<()> {
    std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
            .with_context(|| format!("restricting {} to its owner", dir.display()))?;
    }
    #[cfg(windows)]
    {
        if dir.starts_with(default_config_path().parent().unwrap_or(Path::new(""))) {
            let status = std::process::Command::new("icacls")
                .arg(dir)
                .args([
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-5-18:(OI)(CI)F",
                    "/grant:r",
                    "*S-1-5-32-544:(OI)(CI)F",
                    "/q",
                ])
                .status();
            match status {
                Ok(s) if s.success() => {}
                _ => tracing::warn!(
                    "could not restrict {} to SYSTEM and Administrators; restrict it by hand",
                    dir.display()
                ),
            }
        }
    }
    Ok(())
}

/// Write a file only its owner can read.
pub fn write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let _ = std::fs::remove_file(&tmp);
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)
            .with_context(|| format!("writing {}", tmp.display()))?;
        f.write_all(bytes)
            .with_context(|| format!("writing {}", tmp.display()))?;
    }
    #[cfg(not(unix))]
    {
        std::fs::write(&tmp, bytes).with_context(|| format!("writing {}", tmp.display()))?;
    }
    std::fs::rename(&tmp, path).with_context(|| format!("writing {}", path.display()))
}

/// The commented template of a new relay.toml.
fn template(server_url: &str, name: Option<&str>) -> String {
    let name_line = match name {
        Some(n) => format!("name = {}\n", toml_edit::Value::from(n)),
        None => "# name = \"paris-dc-1\"\n".to_string(),
    };
    format!(
        r#"# XTM One Relay configuration. Every key can also be set by an
# XTM_RELAY_<KEY> environment variable. `xtm-one-relay check-config` validates
# this file; a running relay reloads allowed_destinations on SIGHUP or when the
# file changes.

# The XTM One platform this relay is enrolled with.
server_url = {server}

# How the relay shows in XTM One.
{name_line}
# The destinations this relay may reach - the hard boundary, checked on THIS
# machine after DNS resolution. Host names, "*.domain", IPs and CIDRs, each with
# an optional ":port". Empty refuses everything; "*" reaches any host except
# this one, link-local and cloud metadata addresses.
allowed_destinations = []

# Destinations whose TLS certificate is not verified (self-signed appliances).
# Prefer ca_bundle.
# insecure_tls_destinations = ["tanium.corp.local"]

# Extra CA certificates (PEM) trusted for destinations and the platform.
# ca_bundle = "/etc/xtm-one-relay/ca.pem"

# An HTTP(S) proxy to reach the platform (destinations are reached directly).
# upstream_proxy = "http://proxy.corp.local:3128"

# How many requests this relay serves at once (1-256).
# max_concurrent_requests = 32

# auto (WebSocket, long-poll fallback), websocket or long_poll.
# transport = "auto"

# A local health endpoint for probes: GET /healthz.
# health_listen = "127.0.0.1:8411"

# Apply the signed relay builds the platform offers.
# auto_update = true

# error, warn, info, debug or trace.
# log_level = "info"
"#,
        server = toml_edit::Value::from(server_url),
    )
}

/// Record the platform (and the name) enrollment used in relay.toml, keeping
/// everything else of the file as written - comments included.
pub fn write_enrollment(path: &Path, server_url: &str, name: Option<&str>) -> Result<()> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            if let Some(dir) = path.parent() {
                prepare_dir(dir)?;
            }
            return write_private(path, template(server_url, name).as_bytes());
        }
        Err(err) => {
            return Err(anyhow::Error::new(err).context(format!("reading {}", path.display())))
        }
    };
    let mut doc: toml_edit::DocumentMut = text
        .parse()
        .with_context(|| format!("reading {}", path.display()))?;
    doc["server_url"] = toml_edit::value(server_url);
    if let Some(name) = name {
        doc["name"] = toml_edit::value(name);
    }
    write_private(path, doc.to_string().as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("xtm-relay-config-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_missing_file_is_the_defaults() {
        let dir = temp_dir("missing");
        let config = Config::from_file(&dir.join("relay.toml"), FileConfig::default()).unwrap();
        assert_eq!(config.max_concurrent_requests, DEFAULT_MAX_CONCURRENT);
        assert_eq!(config.transport, TransportMode::Auto);
        assert!(config.allowed_destinations.is_empty());
        assert_eq!(config.dir, dir);
    }

    #[test]
    fn an_unknown_key_is_refused_rather_than_ignored() {
        let err = toml::from_str::<FileConfig>("alowed_destinations = []").unwrap_err();
        assert!(err.to_string().contains("alowed_destinations"), "{err}");
    }

    #[test]
    fn bounds_and_shapes_are_checked() {
        let file = FileConfig {
            max_concurrent_requests: Some(0),
            ..FileConfig::default()
        };
        assert!(Config::from_file(Path::new("relay.toml"), file).is_err());
        let file = FileConfig {
            transport: Some("carrier-pigeon".into()),
            ..FileConfig::default()
        };
        assert!(Config::from_file(Path::new("relay.toml"), file).is_err());
        let file = FileConfig {
            health_listen: Some("8411".into()),
            ..FileConfig::default()
        };
        assert!(Config::from_file(Path::new("relay.toml"), file).is_err());
    }

    #[test]
    fn the_server_url_must_carry_a_credential_safely() {
        assert!(require_secure_server_url("https://xtm.example.com").is_ok());
        assert!(require_secure_server_url("http://localhost:4000").is_ok());
        assert!(require_secure_server_url("http://127.0.0.1:4000").is_ok());
        assert!(require_secure_server_url("http://[::1]:4000").is_ok());
        assert!(require_secure_server_url("http://xtm.example.com").is_err());
        assert!(require_secure_server_url("ftp://xtm.example.com").is_err());
        assert!(require_secure_server_url("not a url").is_err());
    }

    #[test]
    fn lists_split_on_commas_and_spaces() {
        assert_eq!(
            split_list(" a.corp:443, 10.0.0.0/8  b.corp "),
            vec!["a.corp:443", "10.0.0.0/8", "b.corp"]
        );
    }

    #[test]
    fn enrollment_writes_a_template_then_keeps_the_admin_s_file() {
        let dir = temp_dir("enroll");
        let path = dir.join("relay.toml");
        write_enrollment(&path, "https://one.example.com", Some("dc-1")).unwrap();
        let first = Config::load(&path).unwrap();
        assert_eq!(first.server_url.as_deref(), Some("https://one.example.com"));
        assert_eq!(first.name.as_deref(), Some("dc-1"));
        assert!(first.allowed_destinations.is_empty());

        let edited = std::fs::read_to_string(&path).unwrap().replace(
            "allowed_destinations = []",
            "# ours\nallowed_destinations = [\"splunk.corp:8089\"]",
        );
        std::fs::write(&path, edited).unwrap();
        write_enrollment(&path, "https://two.example.com", None).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.contains("# ours"));
        let second = Config::load(&path).unwrap();
        assert_eq!(
            second.server_url.as_deref(),
            Some("https://two.example.com")
        );
        assert_eq!(second.name.as_deref(), Some("dc-1"));
        assert_eq!(second.allowed_destinations, vec!["splunk.corp:8089"]);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
