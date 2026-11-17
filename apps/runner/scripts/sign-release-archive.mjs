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

// Sign a runner release archive with the release key, as `tauri signer sign`
// does, with the runner version in the signed trusted comment.
//
// `tauri signer sign` writes "timestamp:<t>\tfile:<name>" and takes no other
// comment. The install scripts and the self-updater also compare a signed
// "version:<v>" with the version the platform ships, which is what tells an
// older signed build served in its place from the one expected; the archive
// names are the same in every release, so nothing else in the signature can.
//
// Usage: node sign-release-archive.mjs <archive> <runner version>
//
// The key is read the way tauri signer reads it: TAURI_SIGNING_PRIVATE_KEY
// (the base64 of the whole minisign secret key file, or the path of that file)
// and TAURI_SIGNING_PRIVATE_KEY_PASSWORD. Writes <archive>.minisig, minisign's
// own four-line text, after checking both signatures it made. Node's crypto
// only, so it runs the same on every leg of the release.

import { createHash, createPrivateKey, createPublicKey, scryptSync, sign, verify } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";

// PKCS#8 DER prefix of an Ed25519 private key, before its 32-byte seed.
const PKCS8_ED25519 = Buffer.from("302e020100300506032b657004220420", "hex");

function fail(message) {
  process.stderr.write(`error: ${message}\n`);
  process.exit(1);
}

// rust-minisign's raw_scrypt_params (libsodium's pickparams): the scrypt
// parameters a secret key's opslimit and memlimit stand for.
function scryptParams(opslimit, memlimit) {
  if (memlimit > 1073741824n) fail("the secret key asks for scrypt parameters too high");
  const ops = opslimit < 32768n ? 32768n : opslimit;
  const r = 8n;
  let nLog2 = 1n;
  let p;
  const pickN = (maxN) => {
    while (nLog2 < 63n && !((1n << nLog2) > maxN / 2n)) nLog2 += 1n;
  };
  if (ops < memlimit / 32n) {
    p = 1n;
    pickN(ops / (r * 4n));
  } else {
    pickN(memlimit / (r * 128n));
    let maxrp = BigInt.asUintN(32, ops / 4n / (1n << nLog2));
    if (maxrp > 0x3fffffffn) maxrp = 0x3fffffffn;
    p = maxrp / r;
  }
  if (nLog2 > 20n || p < 1n) fail("the secret key asks for scrypt parameters out of range");
  return { N: Number(1n << nLog2), r: Number(r), p: Number(p) };
}

// The key id and the Ed25519 key of a minisign secret key as tauri signer
// generate writes it: "Ed", kdf "Sc", checksum "B2", salt, opslimit, memlimit,
// then key id + secret key (seed and public key) + checksum, sealed with the
// scrypt stream of the password - an empty one too: a key generated without a
// password is sealed all the same. Only a key with no kdf is left clear.
function secretKey(value, password) {
  const encoded = existsSync(value) ? readFileSync(value, "utf8") : value;
  const lines = Buffer.from(encoded.trim(), "base64").toString("utf8").split(/\r?\n/);
  const raw = Buffer.from((lines[1] ?? "").trim(), "base64");
  const kdf = raw.toString("latin1", 2, 4);
  if (raw.length !== 158 || raw.toString("latin1", 0, 2) !== "Ed" || (kdf !== "Sc" && kdf !== "\0\0")) {
    fail("TAURI_SIGNING_PRIVATE_KEY is not a minisign secret key");
  }
  const sealed = Buffer.from(raw.subarray(54));
  if (kdf === "Sc") {
    const { N, r, p } = scryptParams(raw.readBigUInt64LE(38), raw.readBigUInt64LE(46));
    const stream = scryptSync(Buffer.from(password, "utf8"), raw.subarray(6, 38), sealed.length, {
      N,
      r,
      p,
      maxmem: 256 * N * r + 128 * r * p + (1 << 20),
    });
    for (let i = 0; i < sealed.length; i++) sealed[i] ^= stream[i];
  }
  const keyId = sealed.subarray(0, 8);
  const privateKey = createPrivateKey({
    key: Buffer.concat([PKCS8_ED25519, sealed.subarray(8, 40)]),
    format: "der",
    type: "pkcs8",
  });
  const publicKey = createPublicKey(privateKey);
  // The seed gives back the public half the key carries only when the password opened it.
  if (!publicKey.export({ format: "der", type: "spki" }).subarray(-32).equals(sealed.subarray(40, 72))) {
    fail("TAURI_SIGNING_PRIVATE_KEY_PASSWORD does not open TAURI_SIGNING_PRIVATE_KEY");
  }
  return { keyId, privateKey, publicKey };
}

const [archive, version] = process.argv.slice(2);
if (!archive || !version) fail("usage: sign-release-archive.mjs <archive> <runner version>");
if (!/^[0-9]+\.[0-9]+\.[0-9]+$/.test(version)) fail(`runner version '${version}' is not MAJOR.YYMMDD.SEQ`);
const name = basename(archive);
if (/[\t\r\n]/.test(name)) fail(`the archive name ${JSON.stringify(name)} cannot go into a trusted comment`);
const keyValue = (process.env.TAURI_SIGNING_PRIVATE_KEY ?? "").trim();
if (!keyValue) fail("TAURI_SIGNING_PRIVATE_KEY is not set");

const { keyId, privateKey, publicKey } = secretKey(keyValue, process.env.TAURI_SIGNING_PRIVATE_KEY_PASSWORD ?? "");
const digest = createHash("blake2b512").update(readFileSync(archive)).digest();
const signature = sign(null, digest, privateKey);
const comment = `timestamp:${Math.floor(Date.now() / 1000)}\tfile:${name}\tversion:${version}`;
const signedComment = Buffer.concat([signature, Buffer.from(comment, "utf8")]);
const globalSignature = sign(null, signedComment, privateKey);
if (!verify(null, digest, publicKey, signature) || !verify(null, signedComment, publicKey, globalSignature)) {
  fail("the signature does not verify under the key that made it");
}
writeFileSync(
  `${archive}.minisig`,
  "untrusted comment: signature from tauri secret key\n" +
    `${Buffer.concat([Buffer.from("ED", "latin1"), keyId, signature]).toString("base64")}\n` +
    `trusted comment: ${comment}\n` +
    `${globalSignature.toString("base64")}\n`,
);
const printedId = Buffer.from(keyId).reverse().toString("hex").toUpperCase().replace(/^0+(?=.)/, "");
process.stdout.write(`${name}.minisig: signed with key ID ${printedId} for runner ${version}\n`);
