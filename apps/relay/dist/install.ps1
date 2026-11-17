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
    Install, enroll and start the XTM One Relay as a Windows service.

.DESCRIPTION
    Every platform release image embeds this script and serves it at
    <instance>/api/v1/relays/dist/xtm-one-relay-installer.ps1 next to the
    version-matched relay archives built by the same release pipeline. It:

      1. Detects the CPU architecture (x64 / arm64).
      2. Resolves the version-matched signed archive through the platform
         (-Server) or an explicit -DownloadBase mirror (air-gapped setups).
      3. Downloads it, verifies its SHA-256 and its release signature (the
         .minisig published next to it) and installs the binary.
      4. Writes relay.toml (%ProgramData%\XTM One Relay) with the platform
         URL and the relay's local allow-list when there is none yet.
      5. With -Server and -Key: enrolls once and installs the xtm-one-relay
         Windows service (LocalSystem, automatic start, restarted on failure).

    Pull-only by design: the relay dials OUT to XTM One over HTTPS. No
    inbound port is ever opened on this machine.

.EXAMPLE
    # From an elevated PowerShell:
    powershell -ExecutionPolicy Bypass -File .\xtm-one-relay-installer.ps1 `
        -Server https://xtm-one.example.com -Key rlenr-XXXX -Destinations 'splunk.corp:8089'
#>
[CmdletBinding()]
param(
    [string]$Server = $env:XTM_ONE_SERVER,
    [string]$Key = $env:XTM_RELAY_ENROLLMENT_KEY,
    [string]$Name = $env:XTM_RELAY_NAME,
    # The relay's local allow-list, comma-separated: host:port, *.corp:443, 10.0.0.0/8:8089.
    [string]$Destinations = $env:XTM_RELAY_ALLOWED_DESTINATIONS,
    [string]$DownloadBase = $env:XTM_RELAY_DOWNLOAD_BASE,
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'XTM One Relay'),
    # Install the binary (and enroll) without creating the service.
    [switch]$NoService,
    [switch]$RequireChecksum,
    [switch]$RequireSignature
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "Warning: $msg" -ForegroundColor Yellow }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this installer from an elevated PowerShell: the relay installs a Windows service."
}
if (@('1', 'true', 'yes') -contains "$env:XTM_RELAY_REQUIRE_CHECKSUM".ToLower()) { $RequireChecksum = [switch]$true }
if (@('1', 'true', 'yes') -contains "$env:XTM_RELAY_REQUIRE_SIGNATURE".ToLower()) { $RequireSignature = [switch]$true }
# The release signing keys: those of apps/runner/release-signing.json, which
# the relay is built and signed with too
# (backend/tests/test_relay_install_scripts.py keeps them equal).
$releasePubkeys = 'RWST68eWcapzC8LRtFi7bOSPsJYBBeav3f0GL5AYvdrYKlKjDQ9sISAq'
# Signatures older than this Unix time are refused (0: none).
$releaseTimestampFloor = 0

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

try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# -- 1. Target detection ------------------------------------------------------
$archRaw = $env:PROCESSOR_ARCHITEW6432
if (-not $archRaw) { $archRaw = $env:PROCESSOR_ARCHITECTURE }
if ($archRaw -eq 'ARM64') { $arch = 'arm64' } else { $arch = 'x64' }
$archive = "xtm-one-relay-windows-$arch.zip"
Write-Step "Installing the XTM One Relay (windows/$arch)"

# -- 2. Resolve the version-matched artifact ----------------------------------
$art = $null
if ($Server) {
    $base = $Server.TrimEnd('/')
    try {
        $m = Invoke-RestMethod -UseBasicParsing -Uri "$base/api/v1/relays/update-manifest"
        $entry = $m.artifacts.windows.$arch
        if ($entry -and $entry.url) {
            $art = @{ Url = $entry.url; Sha256Url = $entry.sha256_url; SignatureUrl = $entry.signature_url; Version = "$($m.relay_version)" }
            Write-Note "Version-matched to the platform via $base"
        } else {
            Write-Warn "The platform at $base serves no windows/$arch relay build (a source/dev build?)."
        }
    } catch {
        Write-Warn "Could not read the relay manifest from $base ($($_.Exception.Message))."
    }
}
if (-not $art -and $DownloadBase) {
    $b = $DownloadBase.TrimEnd('/')
    $art = @{ Url = "$b/$archive"; Sha256Url = "$b/$archive.sha256"; SignatureUrl = "$b/$archive.minisig"; Version = '' }
}
if (-not $art) { throw "No download source: pass -Server <your XTM One URL> or -DownloadBase <mirror URL>." }
if ($art.Url -like 'http://*') {
    Write-Warn "Plain HTTP download ($($art.Url)): the binary and its .sha256 travel unencrypted. Use an https:// URL."
}

# -- 3. Download + verify -----------------------------------------------------
if ($env:XTM_RELAY_MINISIGN_PUBKEY) {
    $keyLine = ConvertTo-MinisignKeyLine $env:XTM_RELAY_MINISIGN_PUBKEY.Trim()
    if (-not $keyLine) { throw "Refusing to install: XTM_RELAY_MINISIGN_PUBKEY is not a minisign public key." }
    $releasePubkeys = $keyLine
    $releaseTimestampFloor = 0
    Write-Warn "XTM_RELAY_MINISIGN_PUBKEY replaces the release keys: signatures are checked against key ID $(Get-MinisignKeyId $keyLine)."
}
$canVerifySignature = Initialize-SignatureVerifier
if (-not $canVerifySignature -and $RequireSignature) {
    throw "Refusing to install an unverified binary: the release signature verifier cannot run on this host (-RequireSignature)."
}
$work = Join-Path $env:TEMP ("xtm-one-relay-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$unverified = $null
$unsigned = $null
try {
    $zipPath = Join-Path $work $archive
    Write-Step "Downloading $($art.Url)"
    Invoke-WebRequest -UseBasicParsing -Uri $art.Url -OutFile $zipPath
    $actual = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()

    $expected = $null
    if ($art.Sha256Url) {
        try {
            $shaBody = (Invoke-WebRequest -UseBasicParsing -Uri $art.Sha256Url).Content
            if ($shaBody -is [byte[]]) { $shaBody = [Text.Encoding]::ASCII.GetString($shaBody) }
            $token = ((("$shaBody".Trim()) -split '\s+')[0]).ToLower()
            if (-not $token) { $unverified = "the .sha256 at $($art.Sha256Url) is empty" }
            elseif ($token -notmatch '^[0-9a-f]{64}$') { throw "Refusing to install: the .sha256 at $($art.Sha256Url) does not hold a SHA-256 digest." }
            else { $expected = $token }
        } catch {
            if ($_.Exception.Message -like 'Refusing*') { throw }
            $unverified = "no .sha256 could be fetched from $($art.Sha256Url)"
        }
    } else {
        $unverified = 'no .sha256 is published next to the archive'
    }
    if ($expected) {
        if ($expected -ne $actual) { throw "SHA-256 mismatch for $archive (expected $expected, got $actual)." }
        Write-Ok "SHA-256 verified."
    } elseif ($RequireChecksum) {
        throw "Refusing to install an unverified binary: $unverified (-RequireChecksum)."
    } else {
        Write-Warn "INTEGRITY NOT VERIFIED: $unverified."
    }

    if (-not $canVerifySignature) {
        $unsigned = 'the release signature verifier cannot run on this host'
    } elseif (-not $art.SignatureUrl) {
        $unsigned = 'no .minisig is published next to the archive'
    } else {
        $sigText = $null
        try {
            $sigBody = (Invoke-WebRequest -UseBasicParsing -Uri $art.SignatureUrl).Content
            if ($sigBody -is [byte[]]) { $sigBody = [Text.Encoding]::UTF8.GetString($sigBody) }
            if ("$sigBody".Length -gt 65536) { $unsigned = "the .minisig at $($art.SignatureUrl) is larger than a signature can be" }
            else { $sigText = ConvertTo-MinisignText "$sigBody" }
        } catch {
            $unsigned = "no .minisig could be fetched from $($art.SignatureUrl)"
        }
        if (-not $unsigned) {
            if (-not $sigText) {
                $unsigned = "the .minisig at $($art.SignatureUrl) is empty"
            } else {
                $reason = $minisignVerifier::Verify([IO.File]::ReadAllBytes($zipPath), $sigText, $releasePubkeys, $archive, $art.Version, $releaseTimestampFloor)
                if ($reason -eq 'not a minisign signature') {
                    $unsigned = "the .minisig at $($art.SignatureUrl) is not a minisign signature"
                } elseif ($reason -eq 'its trusted comment names no file') {
                    $unsigned = "the trusted comment of the signature at $($art.SignatureUrl) names no file"
                } elseif ($reason -eq 'it names another runner version') {
                    throw "Refusing to install: the signature at $($art.SignatureUrl) is for relay $($minisignVerifier::TrustedField($sigText, 'version')), not $($art.Version), the version the platform ships."
                } elseif ($reason) {
                    throw "Refusing to install: the signature at $($art.SignatureUrl) does not verify for $archive ($reason)."
                } else {
                    Write-Ok "Release signature verified (key ID $($minisignVerifier::SignatureKeyId($sigText)))."
                }
            }
        }
    }
    if ($unsigned) {
        if ($RequireSignature) { throw "Refusing to install an unverified binary: $unsigned (-RequireSignature)." }
        Write-Warn "SIGNATURE NOT VERIFIED: $unsigned."
    }

    # -- 4. Install ------------------------------------------------------------
    Expand-Archive -Path $zipPath -DestinationPath $work -Force
    $binSrc = Join-Path $work 'xtm-one-relay.exe'
    if (-not (Test-Path $binSrc)) { throw "The archive did not contain xtm-one-relay.exe" }
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $binDst = Join-Path $InstallDir 'xtm-one-relay.exe'
    $existing = Get-Service -Name 'xtm-one-relay' -ErrorAction SilentlyContinue
    if ($existing -and $existing.Status -ne 'Stopped') {
        Stop-Service -Name 'xtm-one-relay' -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    Copy-Item -Path $binSrc -Destination $binDst -Force
    Write-Ok "Installed to $binDst"
} finally {
    Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
}

# -- 5. Configuration ---------------------------------------------------------
function ConvertTo-TomlString([string]$value) {
    return '"' + ($value.Replace('\', '\\').Replace('"', '\"')) + '"'
}
$configDir = Join-Path $env:ProgramData 'XTM One Relay'
$config = Join-Path $configDir 'relay.toml'
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
if (-not (Test-Path $config)) {
    $lines = @("# XTM One Relay - see 'xtm-one-relay check-config'. Reloaded when it changes.")
    if ($Server) { $lines += "server_url = $(ConvertTo-TomlString $Server.TrimEnd('/'))" }
    if ($Name) { $lines += "name = $(ConvertTo-TomlString $Name)" }
    $items = @("$Destinations" -split '[,\s]+' | Where-Object { $_ } | ForEach-Object { ConvertTo-TomlString $_ })
    $lines += "allowed_destinations = [" + ($items -join ', ') + "]"
    [IO.File]::WriteAllText($config, ($lines -join "`n") + "`n", (New-Object Text.UTF8Encoding($false)))
    Write-Ok "Wrote $config"
    if (-not $items) { Write-Warn "The relay's allow-list is empty: it reaches nothing until you list destinations in $config." }
} elseif ($Destinations) {
    Write-Note "$config exists and was kept: edit allowed_destinations there to change what the relay reaches."
}

# -- 6. Enroll ----------------------------------------------------------------
$enrolled = $false
if ($Server -and $Key) {
    Write-Step "Enrolling with $Server"
    $enrollArgs = @('--config', $config, 'enroll', '--server-url', $Server.TrimEnd('/'), '--key', $Key, '--force')
    if ($Name) { $enrollArgs += @('--name', $Name) }
    & $binDst @enrollArgs
    if ($LASTEXITCODE -ne 0) { throw "Enrollment failed (exit $LASTEXITCODE)." }
    $enrolled = $true
    Write-Ok "Enrolled."
}

# -- 7. Windows service -------------------------------------------------------
if (-not $NoService) {
    $existing = Get-Service -Name 'xtm-one-relay' -ErrorAction SilentlyContinue
    if ($existing) {
        Start-Service -Name 'xtm-one-relay'
        Write-Ok "Service restarted with the new binary."
    } elseif ($enrolled) {
        Write-Step "Installing the Windows service"
        & $binDst --config $config service install
        if ($LASTEXITCODE -ne 0) { throw "Installing the service failed (exit $LASTEXITCODE)." }
        Write-Ok "Service installed and started."
    } else {
        Write-Note "Not enrolled yet: the service is installed once you enroll (re-run with -Server and -Key)."
    }
}

Write-Host ""
if ($unverified) { Write-Host "XTM One Relay installed WITHOUT integrity verification ($unverified)." -ForegroundColor Yellow }
if ($unsigned) { Write-Host "XTM One Relay installed WITHOUT signature verification ($unsigned)." -ForegroundColor Yellow }
if (-not $unverified -and -not $unsigned) { Write-Host "XTM One Relay installed." -ForegroundColor Green }
if ($enrolled) {
    Start-Sleep -Seconds 3
    & $binDst --config $config status
} else {
    Write-Host "Next step - enroll it (copy the command from Relays > a group > Enroll a relay in XTM One):" -ForegroundColor Yellow
    Write-Host "  xtm-one-relay enroll --server-url <YOUR_XTM_ONE_URL> --key <rlenr-...>" -ForegroundColor White
}
