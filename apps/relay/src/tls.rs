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

//! The TLS configurations of the relay, built once.
//!
//! Every connection the relay opens - to the platform and to destinations -
//! trusts the operating system's store, the Mozilla roots (so a minimal
//! container image still reaches a public platform) and the extra CA bundle the
//! admin names (`ca_bundle`, an internal PKI). The one exception is explicit:
//! a destination listed in `insecure_tls_destinations` is reached without
//! verifying its certificate (a self-signed appliance), and the relay says so
//! at every start.

use std::path::Path;
use std::sync::Arc;

use anyhow::{bail, Context, Result};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};
use tracing::{debug, warn};

fn provider() -> Arc<CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

/// Make ring the process-wide provider, for any TLS client built without an
/// explicit configuration. Idempotent.
pub fn install_default_provider() {
    let _ = rustls::crypto::ring::default_provider().install_default();
}

/// The certificates of a PEM bundle; refused when it holds none.
pub fn read_ca_bundle(path: &Path) -> Result<Vec<CertificateDer<'static>>> {
    let bytes =
        std::fs::read(path).with_context(|| format!("reading ca_bundle {}", path.display()))?;
    let certs = CertificateDer::pem_slice_iter(&bytes)
        .collect::<Result<Vec<_>, _>>()
        .with_context(|| format!("reading ca_bundle {}", path.display()))?;
    if certs.is_empty() {
        bail!("ca_bundle {} holds no PEM certificate", path.display());
    }
    Ok(certs)
}

fn root_store(ca_bundle: Option<&Path>) -> Result<RootCertStore> {
    let mut roots = RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let native = rustls_native_certs::load_native_certs();
    if !native.errors.is_empty() {
        debug!(
            "some OS certificates could not be read: {:?}",
            native.errors
        );
    }
    let (added, ignored) = roots.add_parsable_certificates(native.certs);
    debug!("trusting {added} OS certificates ({ignored} unusable)");
    if let Some(path) = ca_bundle {
        let (added, ignored) = roots.add_parsable_certificates(read_ca_bundle(path)?);
        if added == 0 {
            bail!("ca_bundle {}: no usable CA certificate", path.display());
        }
        debug!(
            "trusting {added} certificates of {} ({ignored} unusable)",
            path.display()
        );
    }
    Ok(roots)
}

/// The relay's TLS client configurations.
#[derive(Clone)]
pub struct TlsConfigs {
    /// Certificates verified: the platform and every destination by default.
    pub verified: Arc<ClientConfig>,
    /// Certificates NOT verified: `insecure_tls_destinations` only.
    pub unverified: Arc<ClientConfig>,
}

impl TlsConfigs {
    pub fn build(ca_bundle: Option<&Path>) -> Result<Self> {
        let roots = root_store(ca_bundle)?;
        let mut verified = ClientConfig::builder_with_provider(provider())
            .with_safe_default_protocol_versions()
            .context("TLS protocol versions")?
            .with_root_certificates(roots)
            .with_no_client_auth();
        // reqwest and the WebSocket both speak HTTP/1.1.
        verified.alpn_protocols = vec![b"http/1.1".to_vec()];

        let mut unverified = ClientConfig::builder_with_provider(provider())
            .with_safe_default_protocol_versions()
            .context("TLS protocol versions")?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(NoCertificateVerification(provider())))
            .with_no_client_auth();
        unverified.alpn_protocols = vec![b"http/1.1".to_vec()];

        Ok(Self {
            verified: Arc::new(verified),
            unverified: Arc::new(unverified),
        })
    }
}

/// Accepts any certificate, still checking that the server holds the key it
/// presents (the handshake signature), for the destinations an admin listed in
/// `insecure_tls_destinations`.
#[derive(Debug)]
struct NoCertificateVerification(Arc<CryptoProvider>);

impl ServerCertVerifier for NoCertificateVerification {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// Say at start which destinations are reached without certificate checks.
pub fn warn_unverified(destinations: &[String]) {
    if !destinations.is_empty() {
        warn!(
            "TLS certificates are NOT verified for: {} (insecure_tls_destinations)",
            destinations.join(", ")
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_configurations_build_and_speak_http_1_1() {
        let configs = TlsConfigs::build(None).unwrap();
        assert_eq!(configs.verified.alpn_protocols, vec![b"http/1.1".to_vec()]);
        assert_eq!(
            configs.unverified.alpn_protocols,
            vec![b"http/1.1".to_vec()]
        );
    }

    #[test]
    fn a_ca_bundle_without_a_certificate_is_refused() {
        let path = std::env::temp_dir().join(format!("xtm-relay-empty-{}.pem", std::process::id()));
        std::fs::write(&path, "not a certificate").unwrap();
        assert!(read_ca_bundle(&path).is_err());
        assert!(TlsConfigs::build(Some(&path)).is_err());
        let _ = std::fs::remove_file(&path);
        assert!(read_ca_bundle(Path::new("/nonexistent/ca.pem")).is_err());
    }
}
