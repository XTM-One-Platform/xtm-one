# Filigran licensing

## Summary (PRD style)

- **Problem.** Filigran sold one licence per product (OpenCTI, OpenAEV, OpenCRQ, Copilot, XTM One), each signed by its own product CA and checked its own way. Customers running several products held several licences, renewals drifted apart, and XTM One had to reconcile them.
- **Decision (31 Oct 2026).** ONE licence: the **Filigran license**. It covers the XTM One core and the OpenCTI, OpenAEV and OpenCRQ modules. Every other licence kind is deprecated: still read and honoured until it expires, never issued again.
- **Outcome.** One certificate per customer instance, Enterprise Edition by default on every module, accepted by every product either through XTM One (registration) or pasted directly into the product.
- **Non-goals.** No change to the certificate claims or OIDs (every verifier already shipped keeps working), no change to the API field names of XTM One (`xtm_license`, `xtm_license_pem`, `sub_licenses`).

## The model

| Item | Value |
| --- | --- |
| Product label in the licensing service | `Filigran License` (formerly "Filigran XTM") |
| Certificate product claim, OID `1.3.6.1.4.1.62944.20` | `filigran xtm` (unchanged on purpose) |
| Signed by | the XTM CA (`XTM_CA_PEM`, `backend/app/enterprise/xtm_ca.py`) |
| Required identifier | the XTM One instance ID, subject `OU` (`global` allowed) |
| Types, OID `.10` | `standard`, `lts`, `trial`, `nfr`, `ci` |
| OpenCTI module, OID `.50` | JSON array: `["global"]` by default |
| OpenAEV module, OID `.60` | JSON array: `["global"]` by default |
| OpenCRQ module, OID `.90` | JSON array: `["global"]` by default |
| Copilot `.100`, agentic AI `.110`, quotas | unchanged, defaults on |
| Licence model `.120` (17 Nov 2026) | `2` on every licence issued since; absent before (model 1) |

## Licence model 2 (17 Nov 2026)

One licence with its allowance, no submodule. The issuer's form has no module checkbox, no per-module platform IDs and no agentic AI switch: every Filigran license grants Enterprise Edition to OpenCTI, OpenAEV and OpenCRQ and agentic AI by design, and is bound to ONE instance, the subject `OU`: an OpenCTI platform ID (licence uploaded in that OpenCTI), an OpenAEV platform ID (uploaded in that OpenAEV), an XTM One instance ID (uploaded in XTM One and distributed to every module), or `global`. Never an OpenCRQ ID: OpenCRQ takes its Enterprise Edition from XTM One only.

The certificate keeps every existing claim (module extensions written `["global"]`, `.110` written `true`), so every verifier already shipped grants exactly what model 2 means. The model claim `.120` = `2` is what a model-aware reader keys on:

- XTM One: `XTMLicenseInfo.license_model` (1 when absent or malformed), `covers_every_module`; `xtm_sublicensed_ids` answers `["global"]` for every module type on a model 2 licence, so the register answer (`ee_sources`, `xtm_license_pem`), `ee_platform_status` and `/platform/config` `sub_licenses.modules` all say covered.
- A licence without `.120` (every licence issued before 17 Nov 2026) keeps the per-module grants it was issued with: nothing changes for it.

Module side (their own PRs): Enterprise Edition received from XTM One cannot be disabled and its licence cannot be removed in the module; OpenCRQ has no licence input of its own.

**Module coverage (model 1, licences issued before 17 Nov 2026).** Each module is Enterprise Edition by default: the issuer's form shows one "Enterprise Edition" box per module, checked, written as `["global"]` (every instance of that module). Unchecking it writes `[]` (the module is not covered). An "Advanced: restrict to platform IDs" option, hidden by default, writes the listed platform IDs instead (those instances only). Trial and NFR licences are always issued with every module covered.

**Deprecated licence kinds.** In the licensing service the products OpenCTI, OpenAEV, OpenCRQ (formerly OpenGRC), Copilot and XTM One carry the `[DEPRECATED] ` prefix: their licences stay readable, no new one can be created, and a renewal is issued as a Filigran license. The legacy identifier `opengrc` (OID `.90` stays) is still accepted by XTM One for OpenCRQ registrations.

## Where each product reads it

### Path 1: through XTM One (registration)

XTM One holds the Filigran license (Settings, or `ENTERPRISE_LICENSE`). A product that registers receives `ee_sources` and, when the licence covers it, `xtm_license_pem`: the Filigran-signed certificate it verifies itself. The contract, step by step, is [platform-registration-entitlement.md](platform-registration-entitlement.md). A module is covered when its extension contains `global`, or exactly the `platform_id` the product registered with.

### Path 2: pasted directly in the product

A product's own Enterprise Edition licence field also accepts a Filigran license PEM, verified with the same rules as path 1: signature by the pinned XTM CA, product claim `filigran xtm`, the product's own module extension containing `global` or this platform's ID, the XTM One validity rules (types, the `ci` cap, the 90-day grace period). Legacy product licences (claim `opencti`, `openaev` or `opengrc`, signed by the product CA) keep working unchanged, and the product's Enterprise Edition settings say which licence form is in use.

| Product | Path 1 verifier | Path 2 (direct upload) |
| --- | --- | --- |
| OpenCTI | `opencti-graphql/src/modules/xtm/one/xtm-one-license.ts` | Settings > Enterprise Edition licence field |
| OpenAEV | `openaev-api` `XtmLicenseVerifier.java` | the platform licence setting |
| OpenCRQ | `back/src/features/edition/xtm-license.ts` | the edition licence setting |

### What XTM One exposes

`GET /api/v1/platform/config` returns `xtm_license` with `sub_licenses`:

- `opencti_ids`, `openaev_ids`, `opencrq_ids`: the raw extensions, unchanged;
- `modules.<opencti|openaev|opencrq>`: `covered` (bool), `scope` (`global`, `platforms` or `none`) and `platform_ids` (the listed IDs when `scope` is `platforms`). Built by `SubLicenses.from_license` in `backend/app/api/v1/platform.py`, the same reading as `_xtm_sublicense_grants_ee` for the register answer, so the detail and the grant never disagree. A product (OpenCRQ first) reads `covered` instead of re-deriving it.

## Wording

The user-facing name is "Filigran license" (UI, messages, docs, translations). Never "XTM license" or "XTM One license". Code identifiers and API fields that say `xtm_license` stay as they are.

## Code

| Concern | Where |
| --- | --- |
| Parsing and validity | `backend/app/enterprise/licensing.py` (`decode_xtm_license_pem`, `XTMLicenseInfo`) |
| XTM CA | `backend/app/enterprise/xtm_ca.py` |
| Licence held by this platform, module grant | `backend/app/services/xtm_license.py` (`resolve_xtm_license`, `xtm_sublicensed_ids`) |
| Licence detail, per-module coverage | `backend/app/api/v1/platform.py` (`SubLicenses`, `ModuleCoverage`) |
| Tests | `backend/tests/test_filigran_license_coverage.py`, `test_registration_ee_enabled.py`, `test_edition_model.py` |
