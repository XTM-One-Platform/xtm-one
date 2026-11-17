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
    Runs the release-signature verifier of dist/install.ps1 on its own.

.DESCRIPTION
    install.ps1 compiles a minisign verifier (BLAKE2b-512 + Ed25519) with
    Add-Type because Windows PowerShell 5.1 has neither. The installer's own
    tests run under PowerShell 7 on a Linux CI host; this script covers the
    Windows compiler paths (runner-ci runs it under Windows PowerShell 5.1 and
    PowerShell 7 on both Windows legs), and runs anywhere else too. It loads
    only the section between the BEGIN / END MINISIGN VERIFIER markers: no
    download, no install, no process touched. Exits 1 on any failure.
#>
$ErrorActionPreference = 'Stop'

$installer = Join-Path (Join-Path $PSScriptRoot '..') 'dist\install.ps1'
$installer = [IO.Path]::GetFullPath(($installer -replace '\\', [IO.Path]::DirectorySeparatorChar))
$source = [IO.File]::ReadAllText($installer)
$begin = $source.IndexOf('# BEGIN MINISIGN VERIFIER')
$end = $source.IndexOf('# END MINISIGN VERIFIER')
if ($begin -lt 0 -or $end -le $begin) { throw "The verifier markers were not found in $installer" }
. ([scriptblock]::Create($source.Substring($begin, $end - $begin)))

$script:failures = 0
function Assert-That([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) {
        Write-Host "ok   $name"
    } else {
        Write-Host "FAIL $name $detail"
        $script:failures++
    }
}
function ConvertTo-Hex([byte[]]$bytes) { return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '') }
function ConvertFrom-Hex([string]$hex) {
    $bytes = New-Object byte[] ($hex.Length / 2)
    for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($hex.Substring(2 * $i, 2), 16) }
    return ,$bytes
}

Write-Host "PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition))"
Assert-That 'the verifier compiles and passes its self-test' (Initialize-SignatureVerifier)
if (-not $minisignVerifier) {
    Write-Host 'FAIL the verifier type is not available'
    exit 1
}
# Add-Type cannot replace a type: an edited verifier must be compiled under its
# own name, not taken from what this session loaded before (here, an edit to the
# BLAKE2b parameter block that still compiles must fail the self-test).
$original = $minisignVerifierSource
$minisignVerifierSource = $original.Replace('h[0] ^= 0x01010040UL;', 'h[0] ^= 0x01010041UL;')
Assert-That 'the edit under test changes the source' ($minisignVerifierSource -ne $original)
Assert-That 'an edited verifier is compiled again, not reused from an earlier load' (-not (Initialize-SignatureVerifier))
$minisignVerifierSource = $original
Assert-That 'the original verifier is found again' (Initialize-SignatureVerifier)

# BLAKE2b-512 (RFC 7693): one-block, exactly-one-block and multi-block inputs,
# against hashlib.blake2b.
$blake = @(
    @('', '786a02f742015903c6c6fd852552d272912f4740e15847618a86e217f71f5419d25e1031afee585313896444934eb04b903a685b1448b755d56f701afe9be2ce'),
    @('abc', 'ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d17d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923'),
    @(('a' * 128), 'fc6c71f688f43ea7d60817478808f3cac753e61571865c95adbc2d9122c943a76b92c2cb1047ef3fe7bf6e436ec1d0a99a9e5b216780bf7fed9d7ca91d3a8f3b'),
    @(('a' * 1000), 'd6a69459fe93fc6b9537ed4336e5099e0dcca3e97290a412500ed7a0daffb03d80cf3650a20e0591f748e10c3c534945ee83d5f2c9722f1a68d98b8c01af23fd')
)
foreach ($case in $blake) {
    $digest = ConvertTo-Hex ($minisignVerifier::Blake2b512([Text.Encoding]::ASCII.GetBytes($case[0])))
    Assert-That "BLAKE2b-512 of $($case[0].Length) bytes" ($digest -eq $case[1]) $digest
}

# Ed25519 (RFC 8032 section 7.1, TEST 1 to 3: public point, message,
# signature), then the edge cases on TEST 1: S + L (a non-canonical S), one
# bit flipped in R, in S, in the point or in the message, an R off the curve,
# the identity as the point (of small order: it would accept a forgery over
# any message) and an R of small order.
$rfc = @(
    @('d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a', '', 'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
    @('3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c', '72', '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
    @('fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025', 'af82', '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a')
)
for ($i = 0; $i -lt $rfc.Count; $i++) {
    $ok = $minisignVerifier::Ed25519Verify((ConvertFrom-Hex $rfc[$i][0]), (ConvertFrom-Hex $rfc[$i][1]), (ConvertFrom-Hex $rfc[$i][2]))
    Assert-That "Ed25519: RFC 8032 TEST $($i + 1) verifies" $ok
}
$point = ConvertFrom-Hex $rfc[0][0]
$empty = ConvertFrom-Hex ''
$rs = ConvertFrom-Hex $rfc[0][2]
$refused = @(
    @('S + L', 'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901554c8c7872aa064e049dbb3013fbf29380d25bf5f0595bbe24655141438e7a101b'),
    @('an R off the curve', '02000000000000000000000000000000000000000000000000000000000000005fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b')
)
foreach ($case in $refused) {
    Assert-That "Ed25519: $($case[0]) is refused" (-not $minisignVerifier::Ed25519Verify($point, $empty, (ConvertFrom-Hex $case[1])))
}
foreach ($at in 0, 31, 32, 63) {
    $flipped = [byte[]]$rs.Clone()
    $flipped[$at] = $flipped[$at] -bxor 1
    Assert-That "Ed25519: a bit flipped in signature byte $at is refused" (-not $minisignVerifier::Ed25519Verify($point, $empty, $flipped))
}
$otherPoint = [byte[]]$point.Clone()
$otherPoint[0] = $otherPoint[0] -bxor 1
Assert-That 'Ed25519: a bit flipped in the point is refused' (-not $minisignVerifier::Ed25519Verify($otherPoint, $empty, $rs))
Assert-That 'Ed25519: another message is refused' (-not $minisignVerifier::Ed25519Verify($point, (ConvertFrom-Hex '00'), $rs))
$identity = ConvertFrom-Hex '0100000000000000000000000000000000000000000000000000000000000000'
$forgery = ConvertFrom-Hex '58666666666666666666666666666666666666666666666666666666666666660100000000000000000000000000000000000000000000000000000000000000'
Assert-That 'Ed25519: the identity as the point is refused' (-not $minisignVerifier::Ed25519Verify($identity, [Text.Encoding]::ASCII.GetBytes('any message'), $forgery))
# The all-zero encoding is a point of order 4: with h = H(R || A || M) a
# multiple of 4 and R = [S]B, [S]B = R + [h]A holds, and OpenSSL accepts it.
$zeroPoint = ConvertFrom-Hex '0000000000000000000000000000000000000000000000000000000000000000'
$zeroForgery = ConvertFrom-Hex '9ea4e943f930816652884035e4550f65f10085699a813c9f86efbf1f8c731fc2caa024e73b636b85b3c592482d500b6658d07fd4b945d8e4925d63860a1dc406'
Assert-That 'Ed25519: the all-zero point (order 4) is refused' (-not $minisignVerifier::Ed25519Verify($zeroPoint, [Text.Encoding]::ASCII.GetBytes('all-zero key forgery'), $zeroForgery))
# An R of order 8 under a point with an order-8 part: [S]B = R + [h]A holds
# (OpenSSL accepts it); only refusing an R of small order, as libsodium does,
# catches it.
$mixedPoint = ConvertFrom-Hex '7f524c3a9b817ed8dac1c588c8ffbead05ab89bf2400113f78145483a90f03ff'
$smallR = ConvertFrom-Hex 'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037af67ce3342080c7af6f758f665dc7551cffd88316b8aa22c01bfe64036ab7420e'
Assert-That 'Ed25519: an R of small order is refused' (-not $minisignVerifier::Ed25519Verify($mixedPoint, [Text.Encoding]::ASCII.GetBytes('small-order R test message 0'), $smallR))
# "Taming the many EdDSAs" (Chalkias et al., 2020), the 12 edge cases of
# github.com/novifinancial/ed25519-speccheck: small-order and mixed-order keys
# and R, the cofactored / cofactorless split, S >= L, non-canonical R and key.
# libsodium accepts case 3 only.
$speccheck = @(
    @('8c93255d71dcab10e8f379c26200f3c7bd5f09d9bc3068d3ef4edeb4853022b6', 'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa', 'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a0000000000000000000000000000000000000000000000000000000000000000'),
    @('9bd9f44f4dcc75bd531b56b2cd280b0bb38fc1cd6d1230e14861d861de092e79', 'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa', 'f7badec5b8abeaf699583992219b7b223f1df3fbbea919844e3f7c554a43dd43a5bb704786be79fc476f91d3f3f89b03984d8068dcf1bb7dfc6637b45450ac04'),
    @('aebf3f2601a0c8c5d39cc7d8911642f740b78168218da8471772b35f9d35b9ab', 'f7badec5b8abeaf699583992219b7b223f1df3fbbea919844e3f7c554a43dd43', 'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa8c4bd45aecaca5b24fb97bc10ac27ac8751a7dfe1baff8b953ec9f5833ca260e'),
    @('9bd9f44f4dcc75bd531b56b2cd280b0bb38fc1cd6d1230e14861d861de092e79', 'cdb267ce40c5cd45306fa5d2f29731459387dbf9eb933b7bd5aed9a765b88d4d', '9046a64750444938de19f227bb80485e92b83fdb4b6506c160484c016cc1852f87909e14428a7a1d62e9f22f3d3ad7802db02eb2e688b6c52fcd6648a98bd009'),
    @('e47d62c63f830dc7a6851a0b1f33ae4bb2f507fb6cffec4011eaccd55b53f56c', 'cdb267ce40c5cd45306fa5d2f29731459387dbf9eb933b7bd5aed9a765b88d4d', '160a1cb0dc9c0258cd0a7d23e94d8fa878bcb1925f2c64246b2dee1796bed5125ec6bc982a269b723e0668e540911a9a6a58921d6925e434ab10aa7940551a09'),
    @('e47d62c63f830dc7a6851a0b1f33ae4bb2f507fb6cffec4011eaccd55b53f56c', 'cdb267ce40c5cd45306fa5d2f29731459387dbf9eb933b7bd5aed9a765b88d4d', '21122a84e0b5fca4052f5b1235c80a537878b38f3142356b2c2384ebad4668b7e40bc836dac0f71076f9abe3a53f9c03c1ceeeddb658d0030494ace586687405'),
    @('85e241a07d148b41e47d62c63f830dc7a6851a0b1f33ae4bb2f507fb6cffec40', '442aad9f089ad9e14647b1ef9099a1ff4798d78589e66f28eca69c11f582a623', 'e96f66be976d82e60150baecff9906684aebb1ef181f67a7189ac78ea23b6c0e547f7690a0e2ddcd04d87dbc3490dc19b3b3052f7ff0538cb68afb369ba3a514'),
    @('85e241a07d148b41e47d62c63f830dc7a6851a0b1f33ae4bb2f507fb6cffec40', '442aad9f089ad9e14647b1ef9099a1ff4798d78589e66f28eca69c11f582a623', '8ce5b96c8f26d0ab6c47958c9e68b937104cd36e13c33566acd2fe8d38aa19427e71f98a473474f2f13f06f97c20d58cc3f54b8bd0d272f42b695dd7e89a8c22'),
    @('9bedc267423725d473888631ebf45988bad3db83851ee85c85e241a07d148b41', 'f7badec5b8abeaf699583992219b7b223f1df3fbbea919844e3f7c554a43dd43', 'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff03be9678ac102edcd92b0210bb34d7428d12ffc5df5f37e359941266a4e35f0f'),
    @('9bedc267423725d473888631ebf45988bad3db83851ee85c85e241a07d148b41', 'f7badec5b8abeaf699583992219b7b223f1df3fbbea919844e3f7c554a43dd43', 'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffca8c5b64cd208982aa38d4936621a4775aa233aa0505711d8fdcfdaa943d4908'),
    @('e96b7021eb39c1a163b6da4e3093dcd3f21387da4cc4572be588fafae23c155b', 'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', 'a9d55260f765261eb9b84e106f665e00b867287a761990d7135963ee0a7d59dca5bb704786be79fc476f91d3f3f89b03984d8068dcf1bb7dfc6637b45450ac04'),
    @('39a591f5321bbe07fd5a23dc2f39d025d74526615746727ceefd6e82ae65c06f', 'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', 'a9d55260f765261eb9b84e106f665e00b867287a761990d7135963ee0a7d59dca5bb704786be79fc476f91d3f3f89b03984d8068dcf1bb7dfc6637b45450ac04')
)
for ($i = 0; $i -lt $speccheck.Count; $i++) {
    $ok = $minisignVerifier::Ed25519Verify((ConvertFrom-Hex $speccheck[$i][1]), (ConvertFrom-Hex $speccheck[$i][0]), (ConvertFrom-Hex $speccheck[$i][2]))
    Assert-That "Ed25519: speccheck case $i $(if ($i -eq 3) { 'verifies' } else { 'is refused' })" ($ok -eq ($i -eq 3))
}

# The self-updater's fixture (apps/runner/src/update.rs, throwaway key).
# The fixture's PUBLIC key line (minisign "Ed" + key id + key): not a secret.
$fixtureSigner = 'RWSRNhxpZP3VeNdBcMrZ70t+ks6N46DKBb15Q9yCGtYii+76LXWRTd7T'
$payload = [Text.Encoding]::ASCII.GetBytes("xtm-one-runner self-update fixture payload (#3743)`n")
$raw = "untrusted comment: signature from xtm-one-runner test key`n" +
    "RUSRNhxpZP3VePJ9Gt9CA/TxR2O4oPCtO+vp0gPyMUuTHLwleciB4JQoQFkyfAEAqKvv/4uCXCk1p6SLNm+S9YkD2H3+J9DKiw8=`n" +
    "trusted comment: timestamp:1790000000`tfile:fixture.tar.gz`n" +
    "TEKrt719BBzN00ESdP6yW2DruNkJT1VDUmEae/USsn3IZgs0fPyo8EMEjIbyfPxzudZZA0pwtIK9UD8mT6wyCA==`n"
$tauri = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($raw))
$tampered = [byte[]]$payload.Clone()
$tampered[0] = $tampered[0] -bxor 1
$otherKeyBytes = [Convert]::FromBase64String($fixtureSigner)
$otherKeyBytes[2] = $otherKeyBytes[2] -bxor 1
$otherKey = [Convert]::ToBase64String($otherKeyBytes)
# The same signature relabelled with the legacy "Ed" algorithm id.
$rawLines = $raw -split "`n"
$legacyBytes = [Convert]::FromBase64String($rawLines[1])
$legacyBytes[1] = 0x64
$legacy = ($rawLines[0], [Convert]::ToBase64String($legacyBytes), $rawLines[2], $rawLines[3]) -join "`n"
# What a mirror may do to the text without changing a signed byte, and base64
# that decoders read differently; install.sh reaches the same verdict on each.
$spaced = ($rawLines[0], ($rawLines[1].Substring(0, 50) + ' ' + $rawLines[1].Substring(50) + ' '), $rawLines[2],
    ($rawLines[3].Substring(0, 40) + "`t" + $rawLines[3].Substring(40))) -join "`n"
$alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
$dataChars = $rawLines[1].TrimEnd('=')
$looseLine = $dataChars.Substring(0, $dataChars.Length - 1) + $alphabet[$alphabet.IndexOf($dataChars[-1]) -bor 1] +
    $rawLines[1].Substring($dataChars.Length)
$looseBits = ($rawLines[0], $looseLine, $rawLines[2], $rawLines[3]) -join "`n"
$unpadded = ($rawLines[0], $dataChars, $rawLines[2], $rawLines[3]) -join "`n"

# A signature made by the release tool itself: tauri-cli 2.11.4 `tauri signer
# sign`, with a throwaway key whose private half was deleted. Its trusted
# comment names the file it signed (timestamp:<t><TAB>file:<name>). The
# signer's PUBLIC half is below as tauri.conf.json carries a key: base64 of
# the whole .pub file.
$tauriSignerFile = 'dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDcwRTkxRTBDOTNEMjY0MjMKUldRalpOS1REQjdwY0VSVXVCeVd0enRnMVUzVWVOQ0k1RTU2MTdENGIrd3B2NUFRM0VpREREQUQK'
$tauriSigned = 'dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVRalpOS1REQjdwY0FZZ3BCWTRuZWRjVzJVeC9kRThDTFVrMFM1RzZVRlFHRVZmYXU3NWNLdDhxTnk4Uld0cUFMMERUM2lCMUhuY3BDQU5NN1h2MVJBREE5SzBsUzk1cGdJPQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzkwNTEyMjI0CWZpbGU6eHRtLW9uZS1ydW5uZXItbGludXgteDY0LnRhci5negpYWEl3dGxnRFJuUGlxWm5HNGFCZWQ5dlpHdHNwN2QzbzQwWm40cEhOTkc1c1A1WE1nSi95alF2WWxiaUhITldOQkI3WHFKR2FVSVhhWXU3ZkVucG5EQT09Cg=='
$tauriPayload = [Text.Encoding]::ASCII.GetBytes("xtm-one-runner install-script fixture archive (#4158)`n")
$tauriSigner = ConvertTo-MinisignKeyLine $tauriSignerFile
$tauriText = ConvertTo-MinisignText $tauriSigned

$keyForms = @(
    @('a bare key line is read as it is', $fixtureSigner, $fixtureSigner),
    @('the tauri.conf.json form gives its key line', $tauriSignerFile, 'RWQjZNKTDB7pcERUuByWtztg1U3UeNCI5E5617D4b+wpv5AQ3EiDDDAD'),
    @('the .pub file text gives its key line', [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($tauriSignerFile)), 'RWQjZNKTDB7pcERUuByWtztg1U3UeNCI5E5617D4b+wpv5AQ3EiDDDAD'),
    @('a signature is not a key', $rawLines[1], $null),
    @('text is not a key', 'not a key at all', $null),
    @('the tauri.conf.json form followed by other characters is not a key', ($tauriSignerFile + '!!'), $null)
)
foreach ($case in $keyForms) {
    $line = ConvertTo-MinisignKeyLine $case[1]
    Assert-That "key: $($case[0])" ($line -eq $case[2]) "(got: $line)"
}
# The key ID shown for an override is the one its .pub file prints in its
# untrusted comment (the self-updater's fixture key, then the tauri-cli one).
$fixturePubFile = 'dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDc4RDVGRDY0NjkxQzM2OTEKUldTUk5oeHBaUDNWZU5kQmNNclo3MHQra3M2TjQ2REtCYjE1UTl5Q0d0WWlpKzc2TFhXUlRkN1QK'
foreach ($pubFile in $fixturePubFile, $tauriSignerFile) {
    $printed = ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($pubFile)) -split "`n")[0] -replace '^untrusted comment: minisign public key: ', ''
    $id = Get-MinisignKeyId (ConvertTo-MinisignKeyLine $pubFile)
    Assert-That "key: the key ID is the one its .pub file prints ($printed)" ($id -eq $printed) "(got: $id)"
}

$cases = @(
    @('the raw four-line text verifies', $payload, $raw, $fixtureSigner, 'fixture.tar.gz', $null),
    @('the Tauri base64 form verifies', $payload, (ConvertTo-MinisignText $tauri), $fixtureSigner, 'fixture.tar.gz', $null),
    @('CRLF line endings verify', $payload, (ConvertTo-MinisignText ($raw -replace "`n", "`r`n")), $fixtureSigner, 'fixture.tar.gz', $null),
    @('a leading byte order mark verifies', $payload, (ConvertTo-MinisignText ([string][char]0xFEFF + $raw)), $fixtureSigner, 'fixture.tar.gz', $null),
    @('a tauri-cli signature verifies', $tauriPayload, $tauriText, $tauriSigner, 'xtm-one-runner-linux-x64.tar.gz', $null),
    @('other bytes are refused', $tampered, $raw, $fixtureSigner, 'fixture.tar.gz', 'does not verify'),
    @('an edited trusted comment is refused', $payload, ($raw -replace 'fixture.tar.gz', 'other.tar.gz'), $fixtureSigner, 'other.tar.gz', 'its trusted comment does not verify'),
    @('a signature for another file is refused', $payload, $raw, $fixtureSigner, 'xtm-one-runner-linux-x64.tar.gz', 'its trusted comment names fixture.tar.gz'),
    @('a tauri-cli signature for another build is refused', $tauriPayload, $tauriText, $tauriSigner, 'xtm-one-runner-windows-x64.zip', 'its trusted comment names xtm-one-runner-linux-x64.tar.gz'),
    @('another key is refused', $payload, $raw, $otherKey, 'fixture.tar.gz', 'signed with another key'),
    @('the legacy algorithm is refused', $payload, $legacy, $fixtureSigner, 'fixture.tar.gz', 'legacy'),
    @('a fifth line is refused', $payload, ($raw + "one line too many`n"), $fixtureSigner, 'fixture.tar.gz', 'not a minisign signature'),
    @('a sign-in page is not a signature', $payload, (ConvertTo-MinisignText '<html>Sign in</html>'), $fixtureSigner, 'fixture.tar.gz', 'not a minisign signature'),
    @('a space and a tab inside the base64 lines verify', $payload, $spaced, $fixtureSigner, 'fixture.tar.gz', $null),
    @('unused bits set in the last base64 character verify', $payload, $looseBits, $fixtureSigner, 'fixture.tar.gz', $null),
    @('blanks before the untrusted comment verify', $payload, (ConvertTo-MinisignText ('  ' + $raw)), $fixtureSigner, 'fixture.tar.gz', $null),
    @('an untrusted comment without its space verifies', $payload, ($raw -replace '^untrusted comment: ', 'untrusted comment:'), $fixtureSigner, 'fixture.tar.gz', $null),
    @('an incomplete last base64 group is not a signature', $payload, $unpadded, $fixtureSigner, 'fixture.tar.gz', 'not a minisign signature'),
    @('characters after the base64 wrapper are not a signature', $payload, (ConvertTo-MinisignText ($tauri + '!!')), $fixtureSigner, 'fixture.tar.gz', 'not a minisign signature')
)
foreach ($case in $cases) {
    $reason = $minisignVerifier::Verify($case[1], $case[2], $case[3], $case[4], $null, 0)
    Assert-That $case[0] ($reason -eq $case[5]) "(got: $reason)"
}

# A list of release keys, the key id a signature names picking one; the
# runner version it names, checked against the one the platform ships; and the
# signature floor. A second throwaway key (its private half was never stored)
# signed the same payload, once with the trusted comment of $raw, once naming a
# runner version too, and once with an R of small order (the identity) in its
# global signature, which the cofactorless equation alone accepts.
$secondSigner = 'RWSkpNA/D0II3fqTapZXAP3nyOTH4SeZ1fTnSe3ZEEmgpJhaRSfeZulf'
$secondSigned = "untrusted comment: signature from a second xtm-one-runner test key`n" +
    "RUSkpNA/D0II3SfiHN41aYmDuhaTOzEh8XDl9zitxxgBh2+M+hr3Vo2Lj3kvhQy98P88tNG2C9bVLcAwsxECJ0ayX3HZevDsLwI=`n" +
    "trusted comment: timestamp:1790000000`tfile:fixture.tar.gz`n" +
    "ZomcPnP/btIjcxHKxhjZcv6+01+cP5EfNd8YfgFnlWHKo3tVpgKTj065nIG90yei/FBEO5M70hHrG4uTkUepBw==`n"
$secondVersioned = "untrusted comment: signature from a second xtm-one-runner test key`n" +
    "RUSkpNA/D0II3SfiHN41aYmDuhaTOzEh8XDl9zitxxgBh2+M+hr3Vo2Lj3kvhQy98P88tNG2C9bVLcAwsxECJ0ayX3HZevDsLwI=`n" +
    "trusted comment: timestamp:1790000100`tfile:fixture.tar.gz`tversion:1.260928.57`n" +
    "5oFTY+D91iqx2oVgoleu8S3uLyxQkL9HUK6YCf5FK1fcJQc9XCWhR8JJRXru+YEkVAI4sfM4C9O9lnDcnKdxBA==`n"
$identityR = "untrusted comment: an R of small order`n" +
    "RUSkpNA/D0II3SfiHN41aYmDuhaTOzEh8XDl9zitxxgBh2+M+hr3Vo2Lj3kvhQy98P88tNG2C9bVLcAwsxECJ0ayX3HZevDsLwI=`n" +
    "trusted comment: timestamp:1790000000`tfile:fixture.tar.gz`n" +
    "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABnZ2b7Bcyn+0DZwC8hqHc5iIzNUgjUQBGV5RMB4rRYBQ==`n"
$bothKeys = "$fixtureSigner $secondSigner"
$listCases = @(
    @('a list verifies a signature by its first key', $raw, $bothKeys, $null, 0, $null),
    @('a list verifies a signature by its second key', $secondSigned, $bothKeys, $null, 0, $null),
    @('a key missing from the list is refused', $secondSigned, $fixtureSigner, $null, 0, 'signed with another key'),
    @('a list with an entry that is not a key is refused', $raw, "$fixtureSigner not-a-key", $null, 0, 'the release key is not a minisign public key'),
    @('an R of small order in the global signature is refused', $identityR, $bothKeys, $null, 0, 'its trusted comment does not verify'),
    @('the version the platform ships verifies', $secondVersioned, $bothKeys, '1.260928.57', 0, $null),
    @('another runner version is refused', $secondVersioned, $bothKeys, '1.261001.3', 0, 'it names another runner version'),
    @('a signed version with none known verifies', $secondVersioned, $bothKeys, $null, 0, $null),
    @('a signature that names no version verifies whatever is shipped', $raw, $bothKeys, '1.261001.3', 0, $null),
    @('a signature made at the floor verifies', $raw, $bothKeys, $null, 1790000000, $null),
    @('a signature older than the floor is refused', $raw, $bothKeys, $null, 1790000001, 'it is older than the signature floor')
)
foreach ($case in $listCases) {
    $reason = $minisignVerifier::Verify($payload, $case[1], $case[2], 'fixture.tar.gz', $case[3], $case[4])
    Assert-That $case[0] ($reason -eq $case[5]) "(got: $reason)"
}
Assert-That 'the signed version is read from the trusted comment' ($minisignVerifier::TrustedField($secondVersioned, 'version') -eq '1.260928.57')
Assert-That 'a signature that names no version has none' ($null -eq $minisignVerifier::TrustedField($raw, 'version'))
$printedFixtureId = ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($fixturePubFile)) -split "`n")[0] -replace '^untrusted comment: minisign public key: ', ''
Assert-That 'the key ID a signature names is printed as its .pub file prints it' ($minisignVerifier::SignatureKeyId($raw) -eq $printedFixtureId) "(got: $($minisignVerifier::SignatureKeyId($raw)))"

if ($script:failures -gt 0) {
    Write-Host "$($script:failures) check(s) failed"
    exit 1
}
Write-Host 'All verifier checks passed'
