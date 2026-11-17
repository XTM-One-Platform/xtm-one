# Platform registration: proving Enterprise Edition

`POST /api/v1/platform/register` is how OpenCTI, OpenAEV and OpenCRQ register
with XTM One. Each product re-calls it periodically, so it is also their
heartbeat - there is no separate heartbeat route. This page is for the people
implementing that call in a product: what the answer says about the product's
edition, and how to verify it instead of believing it. The licence
model itself (one Filigran license, module coverage, direct upload in a
product) is [filigran-licensing.md](filigran-licensing.md).

## Threat model

The product's operator decides which host it calls XTM One - the URL is a line
of the deployment's own configuration, and TLS is satisfied just as well by a
host they own - so every value XTM One states in the answer is only as
trustworthy as that setting. A product therefore trusts nothing but material
signed by Filigran and verified against a trust anchor compiled into its own
build.

## The answer

The call is authenticated with the shared registration token
(`Authorization: Bearer <PLATFORM_REGISTRATION_TOKEN>`), compared in constant
time; while XTM One has no token set, every call is refused. Every refusal is a
`401` whose `detail` says what to fix, except once a client has failed the
check 10 times in a minute: then every call from it, the right token included,
is a `429` with `Retry-After` until the window ends (and a `503` while XTM One
cannot count the failures and rate limits fail closed: the `rate_limit_fail_mode`
security option, `RATE_LIMIT_FAIL_MODE`). Retry
at the next heartbeat either way. The token is checked before the body is
read: a call it refuses gets that `401` or `429` whatever it sends, and only a
call past the check gets the `413` of a body over the size cap or the `422` of
one that does not validate. While XTM One runs with the published default
token, only the heartbeat of a platform already registered, at the
`platform_url` it registered with, is accepted: registering a new platform,
changing that URL, re-registering an unregistered platform and unregistering
are refused - each counting as a failed check - since a registered
`platform_url` is a trusted issuer of the JWTs that sign XTM One users in
(the product's identity, whatever internal URL XTM One reaches it on - see
[Cross-platform token identity](cross-platform-identity.md)), and
that heartbeat keeps the `platform_title` already registered, since the title
is what the agents bound to the platform read in their system prompt. It never
clears, downgrades or swaps the platform's license either: the stored
`enterprise_license_pem`, `license_type` and `platform_id` stay unless the call
brings the stored license's renewal - a license XTM One validates, newer than
the stored certificate (later expiry, or the same expiry issued later) and
issued to the same customer (`O`) and instance binding (`OU`). A registration
holding no valid license takes only one bound to its `platform_id` or to
`global`, the check each product makes on its own license - and one recorded
without a `platform_id` only a `global` one, since the `platform_id` the call
sends is not recorded either. Only the stored certificate's own validity ends
it, read again on every heartbeat under the identifier the registration is
stored with, so a platform still sending a pre-rename identifier keeps a
license issued under the new name. Nor does
that heartbeat change what the platform declares: the `intents` and
`business_vertical` it sends are ignored, the ones registered stay. The answer:

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | string | `active` once registered. |
| `platform_identifier` | string | The identifier as XTM One stores it: the legacy `opengrc` comes back as `opencrq`, `openbas` is kept as sent. |
| `ee_enabled` | boolean | XTM One's view of the product's edition, true exactly when `ee_sources` is not empty. **Advisory, never an entitlement proof.** Unchanged, for the products that read it today. |
| `ee_sources` | array of strings | Every path that granted `ee_enabled`, in this order: `platform_license`, then `xtm_sublicense`. Both can be present; empty when `ee_enabled` is false. |
| `xtm_license_pem` | string or `null` | The proof of `xtm_sublicense`: the Filigran-signed Filigran license certificate. Present exactly when `ee_sources` contains `xtm_sublicense` and `xtm_license_withheld_reason` is `null`, `null` otherwise. |
| `xtm_license_withheld_reason` | string or `null` | Why `xtm_license_pem` is `null` although `ee_sources` contains `xtm_sublicense`, `null` otherwise. One value today, `registration_token_published_default`: XTM One runs with the published default registration token (see below). Read any value as a missing proof. |
| `user_integrations` | integer | Always `1`: the company-managed integration. |
| `version` | string | The XTM One version. |
| `procedure_manifest_wanted` | boolean | OpenCRQ only. `true` when the request reported a `procedure_manifest_fingerprint` XTM One holds no manifest for and carried no `procedure_manifest`: register once more with `procedure_manifest` in the body. Always `false` under the published default registration token, which stores no manifest. |
| `sign_in_client_id` | string or null | The platform's OpenID Connect client id (`xtm-platform-<identifier>`) when it sent `sign_in_redirect_uris` and the registration token is not the published default; `null` otherwise. An empty list removes the client (an absent field changes nothing), as do unregister and purge; the published default token neither creates nor removes it. |

An older XTM One may omit `ee_sources`, `xtm_license_pem` or
`xtm_license_withheld_reason`: read a missing field as empty / `null`.

- **`platform_license`** - the product sent its own enterprise license
  (`enterprise_license_pem`) and XTM One validated it against that product's
  trust anchor. The product holds that license and verifies it itself, as it
  always has: the answer carries nothing more to verify for this path.
- **`xtm_sublicense`** - the Filigran license installed on XTM One sub-licenses the
  product: it lists the `platform_id` the product registered with, or `global`,
  for the product's type. This grant does not depend on the product's own
  license, so the certificate travels even when both paths apply.

`xtm_license_pem` is exactly the certificate XTM One validated for that
decision - `ENTERPRISE_LICENSE` from the environment when it is set, otherwise
the license installed in `Settings` - re-serialised from the parsed certificate:
one `CERTIFICATE` block and nothing else, never the text as it was stored, which
may hold other blocks (a key pasted along with it included). No chain travels:
XTM One verifies the license signature directly against the CA public key and
builds no path, so a license issued through an intermediate certificate never
grants anything, and an intermediate is never part of a decision a product has
to repeat.

## Verifying the entitlement

A product is in Enterprise Edition through XTM One when, and only when,
`xtm_license_pem` passes every step below - the checks XTM One itself runs
(`decode_license_pem` in `backend/app/enterprise/licensing.py`), with the same
outcome. A missing or `null` field, a parse error or any failed step means the
XTM path grants nothing: the product is in Community Edition unless its own
license says otherwise. **Never grant Enterprise Edition from the bare
`ee_enabled`.**

1. **Pin the trust anchor in the build.** The anchor is the XTM CA certificate,
   `XTM_CA_PEM` in `backend/app/enterprise/xtm_ca.py`. Compile a copy into the
   product; never read it from configuration, an environment variable or the
   answer, or whoever controls the URL replaces it as well.

   | Digest | SHA-256 |
   | --- | --- |
   | Certificate (DER) | `43:6E:0B:82:14:0F:43:CA:0E:15:3F:1B:27:AC:E3:B3:88:F1:9F:24:BC:6C:36:6E:C1:24:91:87:45:BC:40:64` |
   | SubjectPublicKeyInfo | `9ef09cc6ab116344afd1a47ae5b777842b8c44fae78d223440d19bb7bc7d84ec` (base64 `nvCcxqsRY0Sv0aR65bd3hCuMRPrnjSI0QNGbt7x9hOw=`) |

   Only the RSA public key (4096 bits) matters: XTM One uses the CA certificate
   as a carrier for it and checks none of its other fields, its own validity
   dates (2026-02-22 to 2036-02-22) included.

2. **Parse exactly one certificate.** Refuse a value that is not a single PEM
   `CERTIFICATE` block. Filigran license certificates, like the CA certificate,
   carry serial number `0`, which RFC 5280 disallows: a parser that enforces the
   positive-serial rule must tolerate it, as XTM One does.

3. **Verify the signature**: the certificate's signature over its
   `tbsCertificate`, with the anchor's public key, RSASSA-PKCS1-v1_5, using the
   certificate's own signature hash algorithm (SHA-256 in practice). Build no
   chain and apply no TLS-style path validation: XTM One does neither.

4. **Check the product and the type**, two custom extensions, read as described
   in [Reading an extension](#reading-an-extension):
   - product, `1.3.6.1.4.1.62944.20` (the legacy `2.14521.4.4.20` is read only
     when the first is absent): exactly `filigran xtm`;
   - type, `1.3.6.1.4.1.62944.10` (legacy `2.14521.4.4.10`): one of `standard`,
     `lts`, `trial`, `nfr`, `ci`. A certificate without it, or with an empty
     value, is a `trial`.

   Leave the subject alone: `O` is the customer name, informational, and `OU` -
   the XTM One instance the license was generated for, or `global` - is not
   checked by XTM One, which passes no instance id to the check. A product must
   not check it either.

5. **Apply the validity dates** exactly as XTM One does, all times in UTC:
   - `start` is `notBefore` and `end` is `notAfter`;
   - for a `ci` license, `end` is **replaced** by
     `min(created + 45 minutes, notBefore + 365 days)` - `notAfter` plays no
     part - where XTM One takes `created` from its own instance creation time.
     A product cannot see that time (and must not trust it if the answer carried
     it), so it uses its **own** instance creation time: the cap OpenCTI already
     applies to its own `ci` licenses. The two agree where `ci` licenses belong,
     a pipeline that creates both instances together;
   - `trial` and `ci`: valid while `start <= now <= end`, with no grace period;
   - `standard`, `lts` and `nfr`: valid while `now < end + 90 days`. The 90
     days are XTM One's grace period - days, not the three calendar months of
     OpenCTI's own license check. The grace branch also catches a certificate
     whose `notBefore` is still in the future, so `start` is not enforced for
     these types: mirror that, or a renewal issued ahead of its start date is
     Enterprise Edition on XTM One and Community Edition on the product.

   XTM One also reports, for display only, a warning when fewer than 90 days
   remain before `end` (except for `trial` and `ci`) and the days left in the
   grace period. Neither affects validity.

6. **Find the product's own grant** in the sub-license extension of its type:

   | Product | Extension | Legacy identifier |
   | --- | --- | --- |
   | OpenCTI | `1.3.6.1.4.1.62944.50` | - |
   | OpenAEV | `1.3.6.1.4.1.62944.60` | `openbas` |
   | OpenCRQ | `1.3.6.1.4.1.62944.90` | `opengrc` |

   The value is a JSON array of strings (XTM One compares an item of another
   JSON type through its Python `str()`, `1` as `"1"`; Filigran writes strings
   only); a missing extension, malformed JSON or anything other than an array
   is an empty list. The product is covered when
   the array contains `global`, or exactly (case-sensitively) the `platform_id`
   it sends in its registration request; a product registering without a
   `platform_id` is covered by `global` only. Read the product's own extension
   and no other: an OpenCTI sub-license never covers an OpenCRQ platform.

### Reading an extension

Filigran writes a custom extension's value (the content of `extnValue`) as the
UTF-8 text itself. XTM One also accepts that text wrapped in a DER string: when
the value is at least three bytes long, its first byte is `0x0C` (UTF8String),
`0x13` (PrintableString), `0x16` (IA5String) or `0x04` (OCTET STRING), and its
second byte, read as a number, equals the number of bytes after it, those two
header bytes are skipped. Any other value - a long-form length, an empty
wrapped string - is read whole, header included. The bytes are then decoded as
UTF-8, an invalid sequence becoming U+FFFD rather than an error, and trimmed of
surrounding whitespace.

### Freshness

Evaluate the proof on every answer: an answer without a proof that verifies
ends the Enterprise Edition this path granted. No answer at all (XTM One
unreachable) is the product's call: keeping the last certificate it verified
and re-applying the date rule to it is sound, since a replayed copy proves
exactly as much (see below).

## What remains

A license is a bearer entitlement, exactly like a product license PEM: whoever
holds a valid certificate covering a platform is entitled to Enterprise Edition
for it, whether a genuine XTM One returned it or not, and nothing revokes a
certificate before it expires (grace period included). What the proof closes is
an answer made up by XTM One, or by any host answering in its name, with no
Filigran-signed license behind it. Two consequences stay open:

- **A `global` grant covers every instance of that product type, anywhere.**
  The certificate names a customer (`O`), but nothing in it ties a `global`
  grant to that customer's instances: a copy served by any host - a fake XTM
  One included - makes any instance of the type Enterprise Edition until the
  certificate expires, grace period included. An explicit `platform_id` grant
  covers the listed instances only. No product-side check narrows a `global`
  grant without refusing legitimate ones; narrowing it is a licensing decision
  (explicit ids rather than `global`).
- **The certificate is also XTM One's own license.** XTM One never compares its
  `OU` (step 4), so it installs on any XTM One, which then runs Enterprise
  Edition with its acts ceilings and its sub-license grants. Every recipient can
  do so: the operators of each sub-licensed product, and anyone holding the
  registration token. The token is the same for every product, the caller
  states its own platform type and `platform_id`, and
  `GET /api/v1/platform/config` publishes every sub-licensed id (see below):
  the token alone obtains the certificate whenever it sub-licenses anything.
  That is why a deployment running the published `docker-compose.yml` fallback
  token (still accepted for the heartbeat of a platform registered before)
  never sends the certificate:
  `xtm_license_pem` stays `null` - `ee_enabled` and `ee_sources` unchanged -
  until a non-default token is set, and XTM One logs one warning per process.
  The answer says so in `xtm_license_withheld_reason`
  (`registration_token_published_default`), so a product can tell its operator
  which setting to fix instead of reporting a bare missing proof.

## Privacy

The certificate carries the customer name, the license type and validity dates,
the agentic quotas and the sub-licensed platform ids of every product type.
None of that is new to a caller: `GET /api/v1/platform/config`, which needs no
authentication, already publishes all of it but the start date (`xtm_license`).
The certificate adds its start date, the XTM One instance it was issued for
(`OU`, or `global`) and its creator - and Filigran's signature over the whole,
which is what turns the list into a license (see above). XTM One returns it
only to a caller that presented the registration token, and only when it
proves the Enterprise Edition of the platform that caller says it is. That
claim is not verified - the token is shared by every product and names none of
them - so the disclosure boundary is the token itself, not the platform
identity.

## Code

| File | What |
| --- | --- |
| `backend/app/api/v1/platform.py` | `RegistrationResponse`, `register_platform()`, `_authorize_registration()` (the check behind a per-client budget of failures), `_verify_registration_token()`, `_refuse_published_default_change()`, `_registered_platform_title()`, `_registration_ee_sources()`, `_xtm_sublicense_grants_ee()` |
| `backend/app/core/registration_token.py` | `registration_token_matches()`, the one constant-time, fail-closed comparison of the token, and `registration_token_state()` |
| `backend/app/enterprise/licensing.py` | `decode_license_pem()` (the rule above) and `LicenseInfo.license_certificate_pem` |
| `backend/app/enterprise/xtm_ca.py` | `XTM_CA_PEM`, the trust anchor |
| `backend/tests/test_registration_license_proof.py` | The contract, driven through the route; it also pins the fingerprint above to `XTM_CA_PEM` |
