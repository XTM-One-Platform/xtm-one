#Requires -Version 5.1
# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

<#
.SYNOPSIS
    Install (and optionally enroll + autostart) the XTM One runner (the daemon inside XTM One Desktop, headless here) on Windows.

.DESCRIPTION
    Every platform release image embeds this script and serves it at
    <instance>/api/v1/runners/dist/xtm-one-runner-installer.ps1 alongside the
    version-matched runner archives built by the same release pipeline (there
    is no external download location). It:

      1. Detects the CPU architecture (x64 / arm64).
      2. Resolves the GENUINE, version-matched signed archive:
         - through the platform when -Server is given (the build embedded in
           that instance's own release); otherwise
         - from an explicit -DownloadBase mirror (air-gapped setups).
      3. Downloads the archive, verifies its SHA-256 and its release signature
         (the .minisig published next to it), and installs the binary.
      4. Adds the install dir to the user PATH so `xtm-one-runner` just works.
      5. If -Server and -Key are given: enrolls once (durable credential is
         stored in Windows Credential Manager) and starts the runner at logon,
         hidden, in the interactive session (required for genuine full-desktop
         screenshots - a session-0 service would capture a black screen).

    Pull-only by design: the runner dials OUT to XTM One. No inbound port is
    ever opened on this machine.

.EXAMPLE
    # Install only, then enroll with the command shown in Profile:
    powershell -ExecutionPolicy Bypass -File .\xtm-one-runner-installer.ps1

.EXAMPLE
    # One-shot: install + enroll + autostart
    powershell -ExecutionPolicy Bypass -File .\xtm-one-runner-installer.ps1 `
        -Server https://one.filigran.io -Key renr-XXXX
#>
[CmdletBinding()]
param(
    # XTM One platform base URL. When set, downloads are resolved through the
    # platform so the runner matches the instance's version.
    [string]$Server = $env:XTM_ONE_SERVER,
    # Enrollment key (renr-...) from the XTM One Desktop page in XTM One. When set with -Server,
    # the installer enrolls and sets up autostart.
    [string]$Key = $env:XTM_ONE_ENROLL_KEY,
    # Friendly runner name (defaults to the hostname).
    [string]$Name,
    # Override the download location (self-hosted / air-gapped mirrors).
    [string]$DownloadBase = $env:XTM_RUNNER_DOWNLOAD_BASE,
    # Where the binary is installed.
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'XTM One\Runner'),
    # Skip creating the logon autostart entry.
    [switch]$NoAutostart,
    # Skip adding the install dir to the user PATH.
    [switch]$NoPathUpdate,
    # Refuse to install a binary whose SHA-256 could not be verified (no, or an
    # empty, .sha256 next to the archive) instead of installing it with a
    # warning. Also enabled by XTM_RUNNER_REQUIRE_CHECKSUM=1 - for MDM /
    # unattended rollouts. A mismatch, or a .sha256 that holds no digest, is
    # refused either way.
    [switch]$RequireChecksum,
    # Refuse to install a binary whose release signature could not be checked
    # (no .minisig next to the archive, one that is not a signature or names no
    # file, or no way to run the verifier on this host) instead of installing
    # it with a warning. Also enabled by
    # XTM_RUNNER_REQUIRE_SIGNATURE=1. A signature that does not verify against
    # the release key is refused either way.
    [switch]$RequireSignature
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # progress bar cripples IWR download speed

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "Warning: $msg" -ForegroundColor Yellow }

if (@('1', 'true', 'yes') -contains "$env:XTM_RUNNER_REQUIRE_CHECKSUM".ToLower()) {
    $RequireChecksum = [switch]$true
}
if (@('1', 'true', 'yes') -contains "$env:XTM_RUNNER_REQUIRE_SIGNATURE".ToLower()) {
    $RequireSignature = [switch]$true
}
# The release signing keys: the keys of apps/runner/release-signing.json, the
# keys every self-updating runner is built with
# (backend/tests/test_runner_install_scripts.py keeps them equal, and the image
# build writes in its own). The key id a signature names picks one, so the next
# key is trusted before the release signs with it. XTM_RUNNER_MINISIGN_PUBKEY
# replaces them, for runners signed with your own key: the key line, or the
# base64 of the whole .pub file as tauri.conf.json has it.
$releasePubkeys = 'RWST68eWcapzC8LRtFi7bOSPsJYBBeav3f0GL5AYvdrYKlKjDQ9sISAq'
# Their signatures whose trusted timestamp is older than this Unix time are
# refused (0: none): the timestamp_floor of release-signing.json.
$releaseTimestampFloor = 0

# Release signature (minisign). The SHA-256 comes from the same server as the
# archive, so it proves the download is intact, not who built it; the .minisig
# does. Windows PowerShell 5.1 has no Ed25519 and .NET no BLAKE2b, so the
# verifier is compiled here: BLAKE2b-512 (RFC 7693) and Ed25519 verification
# (RFC 8032 section 5.1.7), the two primitives of a minisign signature.
# BEGIN MINISIGN VERIFIER
$minisignVerifierSource = @'
using System;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;

public static class XtmOneMinisign
{
    static readonly ulong[] IV = {
        0x6a09e667f3bcc908UL, 0xbb67ae8584caa73bUL, 0x3c6ef372fe94f82bUL, 0xa54ff53a5f1d36f1UL,
        0x510e527fade682d1UL, 0x9b05688c2b3e6c1fUL, 0x1f83d9abfb41bd6bUL, 0x5be0cd19137e2179UL };
    static readonly byte[] Sigma = {
        0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,
        14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3,
        11,8,12,0,5,2,15,13,10,14,3,6,7,1,9,4,
        7,9,3,1,13,12,11,14,2,6,5,10,4,0,15,8,
        9,0,5,7,2,4,10,15,14,1,11,12,6,8,3,13,
        2,12,6,10,0,11,8,3,4,13,7,5,15,14,1,9,
        12,5,1,15,14,13,4,10,0,7,6,3,9,2,8,11,
        13,11,7,14,12,1,3,9,5,0,15,4,8,6,2,10,
        6,15,14,9,11,3,0,8,12,2,13,7,1,4,10,5,
        10,2,8,4,7,6,1,5,15,11,9,14,3,12,13,0 };

    static ulong Rotr(ulong x, int n) { return (x >> n) | (x << (64 - n)); }

    static ulong Le64(byte[] b, int off) {
        ulong r = 0;
        for (int i = 7; i >= 0; i--) { r = (r << 8) | b[off + i]; }
        return r;
    }

    static void Mix(ulong[] v, int a, int b, int c, int d, ulong x, ulong y) {
        v[a] = v[a] + v[b] + x; v[d] = Rotr(v[d] ^ v[a], 32);
        v[c] = v[c] + v[d];     v[b] = Rotr(v[b] ^ v[c], 24);
        v[a] = v[a] + v[b] + y; v[d] = Rotr(v[d] ^ v[a], 16);
        v[c] = v[c] + v[d];     v[b] = Rotr(v[b] ^ v[c], 63);
    }

    static void Compress(ulong[] h, byte[] block, int off, ulong t, bool last) {
        ulong[] m = new ulong[16];
        for (int i = 0; i < 16; i++) { m[i] = Le64(block, off + 8 * i); }
        ulong[] v = new ulong[16];
        for (int i = 0; i < 8; i++) { v[i] = h[i]; v[i + 8] = IV[i]; }
        v[12] ^= t;
        if (last) { v[14] = ~v[14]; }
        for (int r = 0; r < 12; r++) {
            int s = (r % 10) * 16;
            Mix(v, 0, 4, 8, 12, m[Sigma[s + 0]], m[Sigma[s + 1]]);
            Mix(v, 1, 5, 9, 13, m[Sigma[s + 2]], m[Sigma[s + 3]]);
            Mix(v, 2, 6, 10, 14, m[Sigma[s + 4]], m[Sigma[s + 5]]);
            Mix(v, 3, 7, 11, 15, m[Sigma[s + 6]], m[Sigma[s + 7]]);
            Mix(v, 0, 5, 10, 15, m[Sigma[s + 8]], m[Sigma[s + 9]]);
            Mix(v, 1, 6, 11, 12, m[Sigma[s + 10]], m[Sigma[s + 11]]);
            Mix(v, 2, 7, 8, 13, m[Sigma[s + 12]], m[Sigma[s + 13]]);
            Mix(v, 3, 4, 9, 14, m[Sigma[s + 14]], m[Sigma[s + 15]]);
        }
        for (int i = 0; i < 8; i++) { h[i] ^= v[i] ^ v[i + 8]; }
    }

    public static byte[] Blake2b512(byte[] data) {
        ulong[] h = (ulong[])IV.Clone();
        h[0] ^= 0x01010040UL;
        int off = 0;
        ulong t = 0;
        while (data.Length - off > 128) {
            t += 128;
            Compress(h, data, off, t, false);
            off += 128;
        }
        byte[] last = new byte[128];
        Array.Copy(data, off, last, 0, data.Length - off);
        t += (ulong)(data.Length - off);
        Compress(h, last, 0, t, true);
        byte[] result = new byte[64];
        for (int i = 0; i < 8; i++) {
            for (int j = 0; j < 8; j++) { result[8 * i + j] = (byte)(h[i] >> (8 * j)); }
        }
        return result;
    }

    static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
    static readonly BigInteger L = BigInteger.Pow(2, 252) + BigInteger.Parse("27742317777372353535851937790883648493");
    static readonly BigInteger D;
    static readonly BigInteger SqrtM1;
    static readonly BigInteger[] Base;
    static readonly BigInteger[] Identity = { BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero };

    static XtmOneMinisign() {
        D = Mod(-121665 * Inv(121666));
        SqrtM1 = BigInteger.ModPow(2, (P - 1) / 4, P);
        BigInteger gy = Mod(4 * Inv(5));
        BigInteger gx;
        RecoverX(gy, 0, out gx);
        Base = new BigInteger[] { gx, gy, BigInteger.One, Mod(gx * gy) };
    }

    static BigInteger Mod(BigInteger a) { BigInteger r = a % P; return r.Sign < 0 ? r + P : r; }
    static BigInteger Inv(BigInteger a) { return BigInteger.ModPow(Mod(a), P - 2, P); }

    static BigInteger[] Add(BigInteger[] p, BigInteger[] q) {
        BigInteger a = Mod((p[1] - p[0]) * (q[1] - q[0]));
        BigInteger b = Mod((p[1] + p[0]) * (q[1] + q[0]));
        BigInteger c = Mod(2 * p[3] * q[3] * D);
        BigInteger d = Mod(2 * p[2] * q[2]);
        BigInteger e = b - a, f = d - c, g = d + c, h = b + a;
        return new BigInteger[] { Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h) };
    }

    static BigInteger[] Mul(BigInteger s, BigInteger[] p) {
        BigInteger[] q = { BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero };
        while (s.Sign > 0) {
            if (!s.IsEven) { q = Add(q, p); }
            p = Add(p, p);
            s >>= 1;
        }
        return q;
    }

    static bool Same(BigInteger[] p, BigInteger[] q) {
        return Mod(p[0] * q[2] - q[0] * p[2]).IsZero && Mod(p[1] * q[2] - q[1] * p[2]).IsZero;
    }

    static bool RecoverX(BigInteger y, int sign, out BigInteger x) {
        x = BigInteger.Zero;
        if (y >= P) { return false; }
        BigInteger x2 = Mod((y * y - 1) * Inv(D * y * y + 1));
        if (x2.IsZero) { return sign == 0; }
        x = BigInteger.ModPow(x2, (P + 3) / 8, P);
        if (!Mod(x * x - x2).IsZero) { x = Mod(x * SqrtM1); }
        if (!Mod(x * x - x2).IsZero) { return false; }
        if ((int)(x & 1) != sign) { x = P - x; }
        return true;
    }

    static BigInteger FromLe(byte[] b, int off, int len) {
        byte[] t = new byte[len + 1];
        Array.Copy(b, off, t, 0, len);
        return new BigInteger(t);
    }

    static BigInteger[] Decompress(byte[] b, int off) {
        BigInteger y = FromLe(b, off, 32);
        int sign = (int)(y >> 255);
        y &= (BigInteger.One << 255) - 1;
        BigInteger x;
        if (!RecoverX(y, sign, out x)) { return null; }
        return new BigInteger[] { x, y, BigInteger.One, Mod(x * y) };
    }

    static byte[] Slice(byte[] b, int off, int len) {
        byte[] r = new byte[len];
        Array.Copy(b, off, r, 0, len);
        return r;
    }

    static byte[] Concat(byte[] a, byte[] b) {
        byte[] r = new byte[a.Length + b.Length];
        Array.Copy(a, 0, r, 0, a.Length);
        Array.Copy(b, 0, r, a.Length, b.Length);
        return r;
    }

    public static bool Ed25519Verify(byte[] key, byte[] message, byte[] signature) {
        if (key.Length != 32 || signature.Length != 64) { return false; }
        // A key or an R of small order (the identity among them) is refused, as
        // libsodium refuses them: a key of small order accepts forgeries.
        BigInteger[] a = Decompress(key, 0);
        if (a == null || Same(Mul(8, a), Identity)) { return false; }
        BigInteger[] r = Decompress(signature, 0);
        if (r == null || Same(Mul(8, r), Identity)) { return false; }
        BigInteger s = FromLe(signature, 32, 32);
        if (s >= L) { return false; }
        byte[] digest;
        using (SHA512 sha = SHA512.Create()) {
            digest = sha.ComputeHash(Concat(Concat(Slice(signature, 0, 32), key), message));
        }
        BigInteger k = FromLe(digest, 0, 64) % L;
        return Same(Mul(s, Base), Add(r, Mul(k, a)));
    }

    const string Trusted = "trusted comment: ";

    // The four lines of a minisign text, blank lines dropped, or null.
    static string[] Lines(string text) {
        System.Collections.Generic.List<string> lines = new System.Collections.Generic.List<string>();
        foreach (string line in text.Replace("\r", "").Split('\n')) { if (line.Trim().Length > 0) { lines.Add(line); } }
        if (lines.Count != 4 || !lines[0].StartsWith("untrusted comment:", StringComparison.Ordinal) ||
            !lines[2].StartsWith(Trusted, StringComparison.Ordinal)) {
            return null;
        }
        return lines.ToArray();
    }

    // The value of the first name: field of a trusted comment, or null.
    static string Field(string comment, string name) {
        foreach (string field in comment.Split('\t')) {
            if (field.StartsWith(name + ":", StringComparison.Ordinal)) { return field.Substring(name.Length + 1); }
        }
        return null;
    }

    // The value of a field of the trusted comment of a minisign text, or null.
    public static string TrustedField(string text, string name) {
        string[] lines = Lines(text);
        return lines == null ? null : Field(lines[2].Substring(Trusted.Length), name);
    }

    // The key ID a minisign text names, as a .pub file prints key IDs, or null.
    public static string SignatureKeyId(string text) {
        string[] lines = Lines(text);
        if (lines == null) { return null; }
        byte[] sig;
        try { sig = Convert.FromBase64String(lines[1].Trim()); } catch (FormatException) { return null; }
        if (sig.Length != 74) { return null; }
        StringBuilder id = new StringBuilder();
        for (int i = 9; i >= 2; i--) { id.Append(sig[i].ToString("X2")); }
        string printed = id.ToString().TrimStart('0');
        return printed.Length > 0 ? printed : "0";
    }

    // Null when the minisign text signs data the way the release does: with the
    // key line among keyLines (space-separated) whose key id it names, prehashed
    // ("ED", the only algorithm the release uses and the self-updater accepts),
    // and a trusted comment that is intact, names file (tauri signer writes
    // "timestamp:<t>\tfile:<name>"), names version when it names one and version
    // is known (the release adds "version:<v>"), and is no older than floor
    // (0: none). Else why not.
    public static string Verify(byte[] data, string text, string keyLines, string file, string version, long floor) {
        System.Collections.Generic.List<byte[]> keys = new System.Collections.Generic.List<byte[]>();
        foreach (string keyLine in keyLines.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries)) {
            byte[] candidate;
            try { candidate = Convert.FromBase64String(keyLine); } catch (FormatException) { candidate = new byte[0]; }
            if (candidate.Length != 42 || candidate[0] != 'E' || candidate[1] != 'd') { return "the release key is not a minisign public key"; }
            keys.Add(candidate);
        }
        if (keys.Count == 0) { return "the release key is not a minisign public key"; }
        string[] lines = Lines(text);
        if (lines == null) { return "not a minisign signature"; }
        byte[] sig, global;
        try {
            sig = Convert.FromBase64String(lines[1].Trim());
            global = Convert.FromBase64String(lines[3].Trim());
        } catch (FormatException) {
            return "not a minisign signature";
        }
        if (sig.Length != 74 || global.Length != 64 || sig[0] != 'E' || (sig[1] != 'D' && sig[1] != 'd')) {
            return "not a minisign signature";
        }
        byte[] pk = null;
        foreach (byte[] candidate in keys) {
            bool same = true;
            for (int i = 2; i < 10; i++) { if (candidate[i] != sig[i]) { same = false; } }
            if (same) { pk = candidate; break; }
        }
        if (pk == null) { return "signed with another key"; }
        if (sig[1] == 'd') { return "legacy"; }
        byte[] key = Slice(pk, 10, 32);
        byte[] signature = Slice(sig, 10, 64);
        if (!Ed25519Verify(key, Blake2b512(data), signature)) { return "does not verify"; }
        string comment = lines[2].Substring(Trusted.Length);
        if (!Ed25519Verify(key, Concat(signature, Encoding.UTF8.GetBytes(comment)), global)) {
            return "its trusted comment does not verify";
        }
        string named = Field(comment, "file");
        if (named != file) { return "its trusted comment names " + (string.IsNullOrEmpty(named) ? "no file" : named); }
        // Signatures made before versions were name none, and keep verifying.
        string signedVersion = Field(comment, "version");
        if (signedVersion != null && !string.IsNullOrEmpty(version) && signedVersion != version) {
            return "it names another runner version";
        }
        string stamp = Field(comment, "timestamp");
        if (floor > 0 && stamp == null) { return "it names no signing time"; }
        if (floor > 0) {
            bool digits = stamp.Length > 0;
            foreach (char c in stamp) { if (c < '0' || c > '9') { digits = false; } }
            if (!digits || (stamp.Length <= 18 && long.Parse(stamp) < floor)) { return "it is older than the signature floor"; }
        }
        return null;
    }
}
'@

# Read a .minisig in either published form: minisign's own four-line text, or
# (older releases) base64 of it on one line. A byte order mark a mirror
# may add sits before the untrusted comment, which nothing signs.
function ConvertTo-MinisignText([string]$body) {
    $text = $body.Trim().TrimStart([char]0xFEFF)
    if ($text -and -not $text.StartsWith('untrusted comment:', [StringComparison]::Ordinal)) {
        try {
            $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($text -replace '\s', '')))
        } catch { }
    }
    return $text
}

# The key line of a minisign public key given as that line or, as
# tauri.conf.json and the release build carry it, as base64 of the whole .pub
# file (parse_minisign_pubkey in apps/runner/src/update.rs reads both). $null
# when it is neither.
function ConvertTo-MinisignKeyLine([string]$value) {
    $lines = @($value -split "`n")
    try {
        $lines += @([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($value -replace '\s', ''))) -split "`n")
    } catch { }
    foreach ($line in $lines) {
        $line = $line.Trim()
        try { $bytes = [Convert]::FromBase64String($line) } catch { continue }
        if ($bytes.Length -eq 42 -and $bytes[0] -eq 0x45 -and $bytes[1] -eq 0x64) { return $line }
    }
    return $null
}

# A key line's key ID as minisign and the .pub file print it: the
# little-endian bytes as one hex number, no leading zeros.
function Get-MinisignKeyId([string]$keyLine) {
    $bytes = [Convert]::FromBase64String($keyLine)
    $id = (-join ($bytes[9..2] | ForEach-Object { $_.ToString('X2') })).TrimStart('0')
    if ($id) { return $id }
    return '0'
}

# Compile the verifier and trust it only once it accepts a known-good signature
# and rejects the same signature over other bytes (the self-updater's
# test fixture, a throwaway key): a host that cannot compile it (Constrained
# Language Mode, no compiler) then reads as "cannot check", never as "bad".
# The type is named after its source: Add-Type cannot replace a type, so a
# session that loaded another revision of this verifier would keep using it.
function Initialize-SignatureVerifier {
    $script:minisignVerifier = $null
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($minisignVerifierSource))
        $name = 'XtmOneMinisign' + (-join ($digest[0..7] | ForEach-Object { $_.ToString('x2') }))
        $type = $name -as [type]
        if (-not $type) {
            $source = $minisignVerifierSource.Replace('XtmOneMinisign', $name)
            if ($PSVersionTable.PSEdition -eq 'Core') {
                Add-Type -TypeDefinition $source -ErrorAction Stop
            } else {
                Add-Type -TypeDefinition $source -ReferencedAssemblies 'System.Numerics' -ErrorAction Stop
            }
            $type = $name -as [type]
        }
        $fixtureKey = 'RWSRNhxpZP3VeNdBcMrZ70t+ks6N46DKBb15Q9yCGtYii+76LXWRTd7T'
        $fixtureSig = "untrusted comment: signature from xtm-one-runner test key`n" +
            "RUSRNhxpZP3VePJ9Gt9CA/TxR2O4oPCtO+vp0gPyMUuTHLwleciB4JQoQFkyfAEAqKvv/4uCXCk1p6SLNm+S9YkD2H3+J9DKiw8=`n" +
            "trusted comment: timestamp:1790000000`tfile:fixture.tar.gz`n" +
            "TEKrt719BBzN00ESdP6yW2DruNkJT1VDUmEae/USsn3IZgs0fPyo8EMEjIbyfPxzudZZA0pwtIK9UD8mT6wyCA==`n"
        $good = [Text.Encoding]::ASCII.GetBytes("xtm-one-runner self-update fixture payload (#3743)`n")
        $bad = [Text.Encoding]::ASCII.GetBytes("Xtm-one-runner self-update fixture payload (#3743)`n")
        if (($null -eq $type::Verify($good, $fixtureSig, $fixtureKey, 'fixture.tar.gz', $null, 0)) -and
            ($null -ne $type::Verify($bad, $fixtureSig, $fixtureKey, 'fixture.tar.gz', $null, 0))) {
            $script:minisignVerifier = $type
            return $true
        }
        return $false
    } catch {
        return $false
    }
}
# END MINISIGN VERIFIER
# (apps/runner/scripts/test-install-verifier.ps1 runs the section between the
# two markers on its own, under Windows PowerShell 5.1 in runner-ci.)

try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# -- 1. Target detection ------------------------------------------------------
$archRaw = $env:PROCESSOR_ARCHITEW6432
if (-not $archRaw) { $archRaw = $env:PROCESSOR_ARCHITECTURE }
switch ($archRaw) {
    'ARM64' { $arch = 'arm64' }
    default { $arch = 'x64' }
}
# Stable archive name, matched to what the release embeds (one native build
# per architecture; the release pipeline ships both).
$archive = "xtm-one-runner-windows-$arch.zip"
Write-Step "Installing the XTM One runner (windows/$arch)"

# -- 2. Resolve the version-matched artifact + integrity URLs ------------------
# The platform manifest advertises the archives embedded in its own release;
# -DownloadBase is the explicit override for air-gapped mirrors of /dist.
function Get-HttpStatus($err) {
    # Windows PowerShell 5.1 raises a WebException whose Response is an
    # HttpWebResponse; PowerShell 7 an HttpResponseException whose Response is
    # an HttpResponseMessage. Both expose StatusCode; no Response = no answer.
    $resp = $err.Exception.Response
    if ($resp -and $resp.StatusCode) { return [int]$resp.StatusCode }
    return $null
}

function Resolve-Artifact {
    $lastAnswer = $null
    if ($Server) {
        $base = $Server.TrimEnd('/')
        $manifestUrl = "$base/api/v1/runners/download/manifest"
        try {
            $m = Invoke-RestMethod -UseBasicParsing -Uri $manifestUrl
            $entry = $m.artifacts.windows.$arch
            if ($entry -and $entry.url) {
                Write-Note "Version-matched to platform $($m.version) via $base"
                # The archive names are the same in every release: the version
                # the signature names is what tells an older build from this one.
                return @{ Url = $entry.url; Sha256Url = $entry.sha256_url; SignatureUrl = $entry.signature_url; Via = $manifestUrl; Version = "$($m.runner_version)" }
            }
            # A 200 alone proves little: an auth proxy's sign-in page is a 200
            # too, and a release can ship other platforms but not this one.
            if (-not ($m -and $m.PSObject.Properties.Name -contains 'artifacts')) {
                $lastAnswer = 'HTTP 200 without a download manifest'
                Write-Warn "The platform at $base answered HTTP 200 without a runner download manifest: an auth proxy or a sign-in page may be answering for /api/v1/runners/download."
            } elseif (($null -eq $m.artifacts) -or (@($m.artifacts.PSObject.Properties).Count -eq 0)) {
                $lastAnswer = 'HTTP 200, no embedded runner downloads'
                Write-Warn "The platform at $base has no embedded runner downloads (a source/dev build?)."
            } else {
                $lastAnswer = 'HTTP 200 without a windows/' + $arch + ' archive'
                Write-Warn "The platform at $base does not ship a windows/$arch runner in this release."
            }
        } catch {
            # Only a 404 says anything about the platform itself; every other
            # failure is about the path to it and names its status.
            $status = Get-HttpStatus $_
            if ($status) { $lastAnswer = "HTTP $status" } else { $lastAnswer = 'no response' }
            if (-not $status) {
                Write-Warn "Could not reach $base (DNS, TLS or connection refused). Check the URL and the network path."
            } elseif ($status -eq 404) {
                Write-Warn "The platform at $base answered HTTP 404: runners are disabled on this instance, or it predates embedded runner downloads."
            } elseif ($status -eq 401 -or $status -eq 403) {
                Write-Warn "The platform at $base refused the request (HTTP $status): an auth proxy or WAF may be blocking /api/v1/runners/download."
            } elseif ($status -ge 500) {
                Write-Warn "The platform at $base answered HTTP ${status}: it is reachable but not serving downloads right now. This is usually transient - retry, or check the backend and the proxy."
            } else {
                Write-Warn "Unexpected HTTP $status from $base while reading the runner download manifest."
            }
        }
    }
    if ($DownloadBase) {
        $b = $DownloadBase.TrimEnd('/')
        return @{ Url = "$b/$archive"; Sha256Url = "$b/$archive.sha256"; SignatureUrl = "$b/$archive.minisig" }
    }
    if ($Server) {
        throw "No download source (last answer: $lastAnswer from $Server). Retry once the platform serves downloads, or pass -DownloadBase <mirror URL>."
    }
    throw "No download source: pass -Server <your XTM One URL> (release images embed the runner) or -DownloadBase <mirror URL>."
}
$art = Resolve-Artifact
# Over plain HTTP the .sha256 rides the same unauthenticated connection as the
# binary it vouches for, so it cannot catch a swap.
$plainHttp = @($art.Via, $art.Url, $art.Sha256Url) | Where-Object { $_ -like 'http://*' } | Select-Object -First 1
if ($plainHttp) {
    Write-Warn "Plain HTTP download ($plainHttp): the binary and its .sha256 travel unencrypted, so whoever can intercept the connection can replace both and the SHA-256 check still passes. Use an https:// URL."
}

# -- 3. Download + verify -----------------------------------------------------
if ($env:XTM_RUNNER_MINISIGN_PUBKEY) {
    $keyLine = ConvertTo-MinisignKeyLine $env:XTM_RUNNER_MINISIGN_PUBKEY.Trim()
    if (-not $keyLine) {
        throw "Refusing to install: XTM_RUNNER_MINISIGN_PUBKEY is not a minisign public key (give the key line of the .pub file, or the base64 of the whole file as tauri.conf.json has it)."
    }
    $releasePubkeys = $keyLine
    # The floor is the release keys' own.
    $releaseTimestampFloor = 0
    Write-Warn "XTM_RUNNER_MINISIGN_PUBKEY replaces the release keys this script carries: signatures are checked against key ID $(Get-MinisignKeyId $keyLine)."
}
$canVerifySignature = Initialize-SignatureVerifier
if (-not $canVerifySignature -and $RequireSignature) {
    # Nothing to verify with: refuse before downloading anything when asked to.
    throw "Refusing to install an unverified binary: the release signature verifier cannot run on this host (-RequireSignature)."
}
$work = Join-Path $env:TEMP ("xtm-one-runner-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
try {
    $zipPath = Join-Path $work $archive
    Write-Step "Downloading $($art.Url)"
    Invoke-WebRequest -UseBasicParsing -Uri $art.Url -OutFile $zipPath

    $actual = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
    $expected = $null
    $unverified = $null
    if ($art.Sha256Url) {
        $shaText = $null
        try {
            $shaBody = (Invoke-WebRequest -UseBasicParsing -Uri $art.Sha256Url).Content
            if ($shaBody -is [byte[]]) { $shaBody = [Text.Encoding]::ASCII.GetString($shaBody) }
            $shaText = "$shaBody".Trim()
        } catch {
            $status = Get-HttpStatus $_
            if ($status) { $why = "HTTP $status" } else { $why = 'no response' }
            $unverified = "no .sha256 could be fetched from $($art.Sha256Url) ($why)"
        }
        if (-not $unverified) {
            # Read like the self-updater (apps/runner/src/update.rs): the first
            # token - a bare digest or the sha256sum form - in any case.
            $token = (($shaText -split '\s+')[0]).ToLower()
            if (-not $token) {
                $unverified = "the .sha256 at $($art.Sha256Url) is empty"
            } elseif ($token -notmatch '^[0-9a-f]{64}$') {
                throw "Refusing to install: the .sha256 at $($art.Sha256Url) does not hold a SHA-256 digest."
            } else {
                $expected = $token
            }
        }
    } else {
        $unverified = 'no .sha256 is published next to the archive'
    }
    if ($expected) {
        if ($expected -ne $actual) {
            throw "SHA-256 mismatch for $archive (expected $expected, got $actual). Aborting."
        }
        Write-Ok "SHA-256 verified."
    } else {
        if ($RequireChecksum) {
            throw "Refusing to install an unverified binary: $unverified (-RequireChecksum)."
        }
        Write-Warn "INTEGRITY NOT VERIFIED: $unverified. The binary is installed without a checksum check; pass -RequireChecksum (or set XTM_RUNNER_REQUIRE_CHECKSUM=1) to refuse instead."
    }

    # The signature is what ties the archive to the release: a mirror or a
    # proxy able to change the archive can change its .sha256 too, not its
    # .minisig.
    $unsigned = $null
    if (-not $canVerifySignature) {
        $unsigned = 'the release signature verifier cannot run on this host'
    } elseif (-not $art.SignatureUrl) {
        $unsigned = 'no .minisig is published next to the archive'
    } else {
        $sigBody = $null
        # A signature is well under 64 KB: never read more from the server
        # (Invoke-WebRequest would buffer a response of any size).
        $sigLimit = 65536
        $sigTooLarge = $false
        try {
            Add-Type -AssemblyName System.Net.Http
            $client = New-Object System.Net.Http.HttpClient
            try {
                $response = $client.GetAsync($art.SignatureUrl, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
                if (-not $response.IsSuccessStatusCode) {
                    $unsigned = "no .minisig could be fetched from $($art.SignatureUrl) (HTTP $([int]$response.StatusCode))"
                } else {
                    $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                    $buffer = New-Object byte[] ($sigLimit + 1)
                    $total = 0
                    while ($total -lt $buffer.Length) {
                        $read = $stream.Read($buffer, $total, $buffer.Length - $total)
                        if ($read -le 0) { break }
                        $total += $read
                    }
                    if ($total -gt $sigLimit) {
                        $sigTooLarge = $true
                    } else {
                        $sigBody = [Text.Encoding]::UTF8.GetString($buffer, 0, $total)
                    }
                }
            } finally {
                $client.Dispose()
            }
        } catch {
            $unsigned = "no .minisig could be fetched from $($art.SignatureUrl) (no response)"
        }
        if ($sigTooLarge) {
            $unsigned = "the .minisig at $($art.SignatureUrl) is larger than a minisign signature can be"
        }
        if (-not $unsigned) {
            $sigText = ConvertTo-MinisignText "$sigBody"
            if (-not $sigText) {
                $unsigned = "the .minisig at $($art.SignatureUrl) is empty"
            } else {
                $reason = $minisignVerifier::Verify([IO.File]::ReadAllBytes($zipPath), $sigText, $releasePubkeys, $archive, $art.Version, $releaseTimestampFloor)
                if ($reason -eq 'not a minisign signature') {
                    # Not a minisign signature at all (an error page, an SPA
                    # catch-all): counts as a missing one, refused only with
                    # -RequireSignature. Whoever can swap files can delete the
                    # signature instead.
                    $unsigned = "the .minisig at $($art.SignatureUrl) is not a minisign signature"
                } elseif ($reason -eq 'legacy') {
                    throw "Refusing to install: the .minisig at $($art.SignatureUrl) is a legacy (not prehashed) minisign signature, which the release never makes."
                } elseif ($reason -eq 'its trusted comment names no file') {
                    # A trusted comment of the signer's own (minisign -t) may
                    # name no file: it is bound to no archive, not to another one.
                    $unsigned = "the trusted comment of the signature at $($art.SignatureUrl) names no file, so the signature cannot be tied to $archive"
                } elseif ($reason -like 'its trusted comment names *') {
                    throw "Refusing to install: the signature at $($art.SignatureUrl) is not for $archive ($reason) - a signed build of another platform or flavor was served in its place."
                } elseif ($reason -eq 'it names another runner version') {
                    throw "Refusing to install: the signature at $($art.SignatureUrl) is for runner $($minisignVerifier::TrustedField($sigText, 'version')), not $($art.Version), the version the platform ships - another signed build of $archive was served in its place."
                } elseif ($reason -eq 'it names no signing time') {
                    throw "Refusing to install: the signature at $($art.SignatureUrl) names no signing time, so nothing says it is newer than $releaseTimestampFloor, the oldest release signature this script accepts."
                } elseif ($reason -eq 'it is older than the signature floor') {
                    throw "Refusing to install: the signature at $($art.SignatureUrl) was made at $($minisignVerifier::TrustedField($sigText, 'timestamp')), before $releaseTimestampFloor, the oldest release signature this script accepts - an older signed build was served in its place."
                } elseif ($reason) {
                    throw "Refusing to install: the signature at $($art.SignatureUrl) does not verify against the release key ($reason) - $archive was changed after it was signed, or signed with another key."
                } else {
                    Write-Ok "Release signature verified."
                    $keyId = $minisignVerifier::SignatureKeyId($sigText)
                    $signedVersion = $minisignVerifier::TrustedField($sigText, 'version')
                    if ($signedVersion) {
                        Write-Note "Signed with key ID $keyId for runner $signedVersion."
                    } else {
                        Write-Note "Signed with key ID $keyId. The signature names no runner version (signed before release signatures carried one), so it cannot tell this build from an older one."
                    }
                }
            }
        }
    }
    if ($unsigned) {
        if ($RequireSignature) {
            throw "Refusing to install an unverified binary: $unsigned (-RequireSignature)."
        }
        Write-Warn "SIGNATURE NOT VERIFIED: $unsigned. Nothing proves the binary was built by the release; pass -RequireSignature (or set XTM_RUNNER_REQUIRE_SIGNATURE=1) to refuse instead."
    }

    # -- 4. Install ------------------------------------------------------------
    Expand-Archive -Path $zipPath -DestinationPath $work -Force
    $binSrc = Join-Path $work 'xtm-one-runner.exe'
    if (-not (Test-Path $binSrc)) { throw "Archive did not contain xtm-one-runner.exe" }

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $binDst = Join-Path $InstallDir 'xtm-one-runner.exe'
    # Stop a running instance so the file can be replaced (upgrade in place).
    Get-Process -Name 'xtm-one-runner' -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    Copy-Item -Path $binSrc -Destination $binDst -Force
    Write-Ok "Installed to $binDst"

    # -- 5. PATH ---------------------------------------------------------------
    if (-not $NoPathUpdate) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not $userPath) { $userPath = '' }
        if (($userPath -split ';') -notcontains $InstallDir) {
            $newPath = ($userPath.TrimEnd(';') + ';' + $InstallDir).TrimStart(';')
            [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
            Write-Ok "Added to your PATH (open a new terminal to use 'xtm-one-runner')."
        }
        if (($env:Path -split ';') -notcontains $InstallDir) {
            $env:Path = $env:Path.TrimEnd(';') + ';' + $InstallDir
        }
    }
} finally {
    Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
}

# -- 6. Enroll (optional) -----------------------------------------------------
$enrolled = $false
if ($Server -and $Key) {
    Write-Step "Enrolling with $Server"
    $enrollArgs = @('enroll', '--server', $Server, '--key', $Key)
    if ($Name) { $enrollArgs += @('--name', $Name) }
    & $binDst @enrollArgs
    if ($LASTEXITCODE -ne 0) { throw "Enrollment failed (exit $LASTEXITCODE)." }
    $enrolled = $true
    Write-Ok "Enrolled. Credential stored in Windows Credential Manager."
}

# -- 7. Autostart in the interactive session (hidden) -------------------------
if ($enrolled -and -not $NoAutostart) {
    Write-Step "Configuring logon autostart"
    # A .vbs launcher runs the console daemon with a hidden window (Run ..., 0).
    # Placed in the per-user Startup folder: no admin, runs in the interactive
    # session at logon - exactly what full-desktop capture needs.
    $vbsPath = Join-Path $InstallDir 'xtm-one-runner-autostart.vbs'
    $vbsBody = @(
        'Set sh = CreateObject("WScript.Shell")'
        ('sh.Run """' + $binDst + '"" run", 0, False')
    ) -join "`r`n"
    Set-Content -Path $vbsPath -Value $vbsBody -Encoding ASCII
    $startupDir = [Environment]::GetFolderPath('Startup')
    Copy-Item -Path $vbsPath -Destination (Join-Path $startupDir 'xtm-one-runner.vbs') -Force
    # Start it now, hidden, for the current session.
    Start-Process -FilePath 'wscript.exe' -ArgumentList '//nologo', ('"' + $vbsPath + '"')
    Write-Ok "Runner started and will relaunch at logon."
}

Write-Host ""
if ($unverified) {
    Write-Host "XTM One runner installed WITHOUT integrity verification ($unverified)." -ForegroundColor Yellow
}
if ($unsigned) {
    Write-Host "XTM One runner installed WITHOUT signature verification ($unsigned)." -ForegroundColor Yellow
}
if (-not $unverified -and -not $unsigned) {
    Write-Host "XTM One runner installed." -ForegroundColor Green
}
if ($enrolled) {
    Write-Note "Check status any time with:  xtm-one-runner status"
} else {
    Write-Host "Next step - enroll this machine (copy the command from the XTM One Desktop page in XTM One):" -ForegroundColor Yellow
    Write-Host "  xtm-one-runner enroll --server <YOUR_XTM_ONE_URL> --key <renr-...>" -ForegroundColor White
    Write-Host "Then start it with:" -ForegroundColor Yellow
    Write-Host "  xtm-one-runner run" -ForegroundColor White
}
