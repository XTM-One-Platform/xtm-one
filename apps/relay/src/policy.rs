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

//! Where this relay may send a request: its LOCAL allow-list.
//!
//! The platform checks the group's `allowed_destinations` before it queues a
//! request; this is the second, authoritative check, made on the customer's own
//! machine with the customer's own list (`allowed_destinations` in relay.toml),
//! so a compromised or misconfigured platform still cannot use the relay to
//! reach anything else.
//!
//! A pattern is the platform's grammar (`normalize_destination_patterns` in
//! `backend/app/services/relays.py`): `host`, `*.domain`, an IP, a CIDR, `*`,
//! each with an optional `:port`. The check is made on the ADDRESSES the relay
//! is about to connect to, after DNS resolution, so a name that resolves
//! somewhere unexpected (DNS rebinding included) is caught:
//!
//! * an address an IP / CIDR pattern covers is admitted;
//! * every address of a host a name pattern names is admitted;
//! * `*` admits any address except the relay's own host (loopback) and
//!   link-local ones;
//! * a cloud metadata endpoint, an unspecified, multicast or broadcast address
//!   is refused whatever the list says.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

use anyhow::{bail, Result};

/// One parsed destination pattern.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Target {
    Any,
    Host(String),
    Suffix(String),
    Net(Net),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Pattern {
    target: Target,
    port: Option<u16>,
}

/// An IP network (an address and a prefix length).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Net {
    addr: IpAddr,
    prefix: u8,
}

impl Net {
    fn contains(&self, ip: IpAddr) -> bool {
        match (self.addr, canonical(ip)) {
            (IpAddr::V4(net), IpAddr::V4(ip)) => {
                let mask = u32::MAX
                    .checked_shl(32 - u32::from(self.prefix))
                    .unwrap_or(0);
                u32::from(net) & mask == u32::from(ip) & mask
            }
            (IpAddr::V6(net), IpAddr::V6(ip)) => {
                let mask = u128::MAX
                    .checked_shl(128 - u32::from(self.prefix))
                    .unwrap_or(0);
                u128::from(net) & mask == u128::from(ip) & mask
            }
            _ => false,
        }
    }
}

/// An IPv4-mapped IPv6 address read as the IPv4 address it carries.
fn canonical(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(v4) => IpAddr::V4(v4),
            None => IpAddr::V6(v6),
        },
        v4 => v4,
    }
}

fn valid_label(label: &str) -> bool {
    !label.is_empty()
        && label.len() <= 63
        && !label.starts_with('-')
        && !label.ends_with('-')
        && label
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

fn valid_hostname(host: &str) -> bool {
    host.len() <= 253 && host.split('.').all(valid_label)
}

fn parse_port(raw: &str, text: &str) -> Result<u16> {
    match text.parse::<u16>() {
        Ok(port) if port > 0 && text.bytes().all(|b| b.is_ascii_digit()) => Ok(port),
        _ => bail!("'{raw}': the port must be 1-65535"),
    }
}

fn split_port<'a>(raw: &str, text: &'a str) -> Result<(&'a str, Option<u16>)> {
    if let Some(rest) = text.strip_prefix('[') {
        let Some(end) = rest.find(']') else {
            bail!("'{raw}': unclosed '['");
        };
        let host = &rest[..end];
        let after = &rest[end + 1..];
        if after.is_empty() {
            return Ok((host, None));
        }
        let Some(port) = after.strip_prefix(':') else {
            bail!("'{raw}': unexpected text after ']'");
        };
        return Ok((host, Some(parse_port(raw, port)?)));
    }
    if text.matches(':').count() == 1 {
        let (host, port) = text.split_once(':').expect("one colon");
        return Ok((host, Some(parse_port(raw, port)?)));
    }
    Ok((text, None))
}

fn parse_net(raw: &str, text: &str) -> Result<Net> {
    let (addr, prefix) = text.split_once('/').expect("a CIDR has a '/'");
    let addr: IpAddr = addr
        .parse()
        .map_err(|_| anyhow::anyhow!("'{raw}': not a valid network address"))?;
    let max = if addr.is_ipv4() { 32 } else { 128 };
    let prefix: u8 = match prefix.parse() {
        Ok(p) if p <= max && prefix.bytes().all(|b| b.is_ascii_digit()) => p,
        _ => bail!("'{raw}': the prefix length must be 0-{max}"),
    };
    Ok(Net { addr, prefix })
}

fn parse_pattern(raw: &str) -> Result<Pattern> {
    let text = raw.trim().to_ascii_lowercase();
    if text.is_empty() {
        bail!("an empty destination pattern");
    }
    if text == "*" {
        return Ok(Pattern {
            target: Target::Any,
            port: None,
        });
    }
    if text.contains("://") {
        bail!("'{raw}': a destination is a host, '*.domain' or a CIDR, not a URL");
    }
    let head = if text.starts_with('[') {
        text.split(']').next().unwrap_or("")
    } else {
        text.as_str()
    };
    if head.contains('/') {
        // `10.0.0.0/8:443` - a port after an IPv4 CIDR; an IPv6 CIDR carries
        // its port only in the bracketed form.
        let (cidr, port) = if text.starts_with('[') {
            split_port(raw, &text)?
        } else if text.matches(':').count() == 1 {
            let (cidr, port) = text.split_once(':').expect("one colon");
            (cidr, Some(parse_port(raw, port)?))
        } else {
            (text.as_str(), None)
        };
        return Ok(Pattern {
            target: Target::Net(parse_net(raw, cidr)?),
            port,
        });
    }
    let (host, port) = split_port(raw, &text)?;
    if host == "*" {
        return Ok(Pattern {
            target: Target::Any,
            port,
        });
    }
    if let Ok(addr) = host.parse::<IpAddr>() {
        let prefix = if addr.is_ipv4() { 32 } else { 128 };
        return Ok(Pattern {
            target: Target::Net(Net { addr, prefix }),
            port,
        });
    }
    if let Some(suffix) = host.strip_prefix("*.") {
        if !valid_hostname(suffix) {
            bail!("'{raw}': invalid domain after '*.'");
        }
        return Ok(Pattern {
            target: Target::Suffix(suffix.to_string()),
            port,
        });
    }
    if host.contains('*') {
        bail!("'{raw}': '*' is only allowed alone or as a leading '*.'");
    }
    if !valid_hostname(host) {
        bail!("'{raw}': not a valid host name");
    }
    Ok(Pattern {
        target: Target::Host(host.to_string()),
        port,
    })
}

/// A host name as patterns compare it: lowercase, no brackets, no trailing dot.
pub fn normalize_host(host: &str) -> String {
    host.trim()
        .to_ascii_lowercase()
        .trim_start_matches('[')
        .trim_end_matches(']')
        .trim_end_matches('.')
        .to_string()
}

/// Addresses no list can admit: the cloud metadata endpoints (instance
/// credentials live there), and addresses that are not one machine.
fn always_refused(ip: IpAddr) -> bool {
    const METADATA_V4: [Ipv4Addr; 4] = [
        Ipv4Addr::new(169, 254, 169, 254), // AWS, GCP, Azure, OpenStack...
        Ipv4Addr::new(169, 254, 170, 2),   // AWS ECS task credentials
        Ipv4Addr::new(169, 254, 170, 23),  // AWS EKS pod identity
        Ipv4Addr::new(100, 100, 100, 200), // Alibaba Cloud
    ];
    const METADATA_V6: [Ipv6Addr; 2] = [
        Ipv6Addr::new(0xfd00, 0xec2, 0, 0, 0, 0, 0, 0x254), // AWS IMDS over IPv6
        Ipv6Addr::new(0xfd00, 0xec2, 0, 0, 0, 0, 0, 0x23),  // AWS EKS pod identity
    ];
    match canonical(ip) {
        IpAddr::V4(v4) => {
            METADATA_V4.contains(&v4)
                || v4.is_unspecified()
                || v4.is_multicast()
                || v4.is_broadcast()
        }
        IpAddr::V6(v6) => METADATA_V6.contains(&v6) || v6.is_unspecified() || v6.is_multicast(),
    }
}

/// The relay host itself or its link: never reached through `*` alone.
fn host_local(ip: IpAddr) -> bool {
    match canonical(ip) {
        IpAddr::V4(v4) => v4.is_loopback() || v4.is_link_local(),
        IpAddr::V6(v6) => v6.is_loopback() || (v6.segments()[0] & 0xffc0) == 0xfe80,
    }
}

/// Why a destination was refused, in the words the platform shows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Denied(pub String);

impl std::fmt::Display for Denied {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Denied {}

/// A parsed allow-list.
#[derive(Debug, Clone, Default)]
pub struct Policy {
    patterns: Vec<Pattern>,
    raw: Vec<String>,
}

impl Policy {
    /// Parse `patterns`; the error names the first refused entry.
    pub fn parse<S: AsRef<str>>(patterns: &[S]) -> Result<Self> {
        let mut parsed = Vec::new();
        let mut raw: Vec<String> = Vec::new();
        for pattern in patterns {
            let pattern = pattern.as_ref();
            parsed.push(parse_pattern(pattern)?);
            let canonical = pattern.trim().to_ascii_lowercase();
            if !raw.contains(&canonical) {
                raw.push(canonical);
            }
        }
        Ok(Self {
            patterns: parsed,
            raw,
        })
    }

    /// The patterns as declared (canonical, deduplicated) - what the hello
    /// reports to the platform.
    pub fn declared(&self) -> &[String] {
        &self.raw
    }

    /// Nothing is allowed.
    pub fn is_empty(&self) -> bool {
        self.patterns.is_empty()
    }

    /// The list admits every host (`*`).
    pub fn allows_all(&self) -> bool {
        self.patterns.iter().any(|p| p.target == Target::Any)
    }

    fn port_ok(pattern: &Pattern, port: u16) -> bool {
        pattern.port.is_none_or(|p| p == port)
    }

    /// Whether a pattern names `host` itself (not through `*` or an address).
    fn names_host(&self, host: &str, port: u16) -> bool {
        self.patterns.iter().any(|p| {
            Self::port_ok(p, port)
                && match &p.target {
                    Target::Host(h) => h == host,
                    Target::Suffix(s) => host.ends_with(&format!(".{s}")),
                    _ => false,
                }
        })
    }

    fn any_admits(&self, port: u16) -> bool {
        self.patterns
            .iter()
            .any(|p| p.target == Target::Any && Self::port_ok(p, port))
    }

    fn net_admits(&self, ip: IpAddr, port: u16) -> bool {
        self.patterns.iter().any(|p| match &p.target {
            Target::Net(net) => Self::port_ok(p, port) && net.contains(ip),
            _ => false,
        })
    }

    /// Whether `host:port` may be asked for at all, before resolving it: a
    /// name some pattern could admit. The addresses decide in [`Self::admit`].
    pub fn may_resolve(&self, host: &str, port: u16) -> bool {
        let host = normalize_host(host);
        self.names_host(&host, port)
            || self.any_admits(port)
            || self
                .patterns
                .iter()
                .any(|p| matches!(p.target, Target::Net(_)) && Self::port_ok(p, port))
    }

    /// The addresses of `host` (resolved to `addrs`) this relay may connect to
    /// on `port`; refused when none is.
    pub fn admit(
        &self,
        host: &str,
        port: u16,
        addrs: &[IpAddr],
    ) -> Result<Vec<SocketAddr>, Denied> {
        let host = normalize_host(host);
        let named = host.parse::<IpAddr>().is_err() && self.names_host(&host, port);
        let any = self.any_admits(port);
        let mut admitted = Vec::new();
        let mut refused_always = 0;
        for &ip in addrs {
            if always_refused(ip) {
                refused_always += 1;
                continue;
            }
            let ok = self.net_admits(ip, port) || named || (any && !host_local(ip));
            if ok {
                let addr = SocketAddr::new(canonical(ip), port);
                if !admitted.contains(&addr) {
                    admitted.push(addr);
                }
            }
        }
        if !admitted.is_empty() {
            return Ok(admitted);
        }
        if addrs.is_empty() {
            return Err(Denied(format!("{host} resolved to no address")));
        }
        if refused_always == addrs.len() {
            return Err(Denied(format!(
                "{host}:{port} resolves to an address a relay never reaches \
                 (a cloud metadata endpoint, or not a single host)"
            )));
        }
        Err(Denied(format!(
            "{host}:{port} is not in this relay's allowed destinations"
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    fn policy(patterns: &[&str]) -> Policy {
        Policy::parse(patterns).unwrap()
    }

    #[test]
    fn the_grammar_is_the_platform_s() {
        for ok in [
            "*",
            "*:443",
            "splunk.corp.local",
            "splunk.corp.local:8089",
            "*.corp.local",
            "*.corp.local:443",
            "10.0.0.0/8",
            "10.0.0.0/8:443",
            "192.168.1.10",
            "192.168.1.10:8443",
            "[fd00::1]:443",
            "[fd00::/8]:443",
            "fd00::/8",
            "fd00::1",
            "  SPLUNK.Corp.Local  ",
        ] {
            assert!(parse_pattern(ok).is_ok(), "{ok}");
        }
        for bad in [
            "",
            "https://splunk",
            "spl*nk.corp",
            "*corp.local",
            "host:0",
            "host:65536",
            "host:abc",
            "10.0.0.0/33",
            "[fd00::1",
            "[fd00::1]x",
            "-bad.host",
            "under_score.host",
        ] {
            assert!(parse_pattern(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn a_named_host_is_admitted_with_every_address_it_resolves_to() {
        let p = policy(&["splunk.corp.local:8089"]);
        let got = p
            .admit(
                "Splunk.Corp.Local.",
                8089,
                &[ip("10.1.2.3"), ip("10.1.2.4")],
            )
            .unwrap();
        assert_eq!(got.len(), 2);
        assert!(p
            .admit("splunk.corp.local", 443, &[ip("10.1.2.3")])
            .is_err());
        assert!(p
            .admit("other.corp.local", 8089, &[ip("10.1.2.3")])
            .is_err());
    }

    #[test]
    fn a_suffix_matches_subdomains_only() {
        let p = policy(&["*.corp.local"]);
        assert!(p.admit("a.corp.local", 443, &[ip("10.0.0.1")]).is_ok());
        assert!(p.admit("a.b.corp.local", 443, &[ip("10.0.0.1")]).is_ok());
        assert!(p.admit("corp.local", 443, &[ip("10.0.0.1")]).is_err());
        assert!(p.admit("evilcorp.local", 443, &[ip("10.0.0.1")]).is_err());
    }

    #[test]
    fn a_cidr_admits_the_resolved_addresses_it_covers_and_only_those() {
        let p = policy(&["10.0.0.0/8:443"]);
        assert!(p.may_resolve("tanium.corp.local", 443));
        assert!(!p.may_resolve("tanium.corp.local", 80));
        let got = p
            .admit(
                "tanium.corp.local",
                443,
                &[ip("10.4.4.4"), ip("192.168.1.1")],
            )
            .unwrap();
        assert_eq!(got, vec!["10.4.4.4:443".parse().unwrap()]);
        // DNS rebinding: the same name answering outside the network is refused.
        assert!(p
            .admit("tanium.corp.local", 443, &[ip("203.0.113.9")])
            .is_err());
        // The port of the CIDR pattern is the one checked.
        assert!(p.admit("10.4.4.4", 80, &[ip("10.4.4.4")]).is_err());
    }

    #[test]
    fn metadata_endpoints_are_refused_whatever_the_list_says() {
        for list in [
            &["*"][..],
            &["169.254.0.0/16"][..],
            &["metadata.google.internal"][..],
        ] {
            let p = policy(list);
            let err = p
                .admit("metadata.google.internal", 80, &[ip("169.254.169.254")])
                .unwrap_err();
            assert!(err.0.contains("metadata"), "{list:?}: {err}");
        }
        let p = policy(&["*"]);
        assert!(p.admit("x", 80, &[ip("::ffff:169.254.169.254")]).is_err());
        assert!(p.admit("x", 80, &[ip("fd00:ec2::254")]).is_err());
        assert!(p.admit("x", 80, &[ip("0.0.0.0")]).is_err());
        assert!(p.admit("x", 80, &[ip("224.0.0.1")]).is_err());
    }

    #[test]
    fn a_wildcard_never_reaches_the_relay_host_but_a_named_one_may() {
        let any = policy(&["*"]);
        assert!(any.admit("localhost", 8089, &[ip("127.0.0.1")]).is_err());
        assert!(any.admit("x", 80, &[ip("169.254.10.10")]).is_err());
        assert!(any.admit("x", 80, &[ip("::1")]).is_err());
        assert!(any.admit("x", 80, &[ip("fe80::1")]).is_err());
        assert!(any.admit("x", 80, &[ip("10.0.0.1")]).is_ok());
        let named = policy(&["localhost:8089"]);
        assert!(named.admit("localhost", 8089, &[ip("127.0.0.1")]).is_ok());
        let loopback = policy(&["127.0.0.1"]);
        assert!(loopback
            .admit("127.0.0.1", 8089, &[ip("127.0.0.1")])
            .is_ok());
    }

    #[test]
    fn an_empty_list_allows_nothing() {
        let p = policy(&[]);
        assert!(p.is_empty());
        assert!(!p.may_resolve("anything", 443));
        assert!(p.admit("anything", 443, &[ip("10.0.0.1")]).is_err());
    }

    #[test]
    fn declared_patterns_are_canonical_and_deduplicated() {
        let p = policy(&["Splunk.Corp:8089", "splunk.corp:8089", "*"]);
        assert_eq!(p.declared(), &["splunk.corp:8089", "*"]);
        assert!(p.allows_all());
    }
}
