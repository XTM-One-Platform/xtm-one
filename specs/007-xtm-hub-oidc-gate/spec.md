# Feature Specification: XTM Hub sign-in entitlement gate

**Feature Branch**: `feat/2952-xtm-hub-oidc-platform-id`

**Created**: 2026-08-19

**Status**: Draft

**Input**: Gate OIDC sign-in on an XTM Hub platform identifier claim, agreed with the XTM Hub team (JPK, CL).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An unentitled person is turned away cleanly (Priority: P1)

Someone authenticates successfully with the identity provider, but the identity
they present does not entitle them to *this* XTM One instance. They are told,
in plain language, that their account is not authorised here — and no account is
created for them.

**Why this priority**: This is the entire point of the feature. Everything else
is refinement. Today such a person is signed in and provisioned, which is the
defect being closed. It is also independently demonstrable: with nothing else
built, showing that an unentitled sign-in leaves no trace is already the
security outcome.

**Acceptance scenarios**:

1. **Given** the gate is configured and a person's identity does not carry the
   expected instance entitlement, **When** they complete sign-in at the identity
   provider, **Then** they are returned to a readable page explaining they are
   not authorised, **And** no user account exists for them afterwards.
2. **Given** the same conditions, **When** an administrator later reviews the
   record, **Then** they can see that a sign-in was refused, for whom, and when.
3. **Given** an unentitled person who *already has* an account from before,
   **When** they sign in, **Then** they are refused in the same way, and their
   existing account is untouched.

---

### User Story 2 - An entitled person gets the role the Hub says they have (Priority: P2)

A person entitled to this instance signs in and receives the role their
organisation assigned them centrally — administrator or ordinary user — without
anyone in XTM One having to grant it.

**Why this priority**: It is what makes the integration useful rather than
merely safe. It depends on Story 1 being in place (the entitlement must be
established before a role can be read from the same identity), so it follows
rather than leads.

**Acceptance scenarios**:

1. **Given** the gate is configured and a person is entitled and marked as an
   administrator centrally, **When** they sign in, **Then** they can use
   administrator functions in XTM One.
2. **Given** the same person is later no longer marked as an administrator
   centrally, **When** they sign in again, **Then** they no longer have
   administrator functions.
3. **Given** an entitled person who is not marked as an administrator, **When**
   they sign in, **Then** they get ordinary access.

---

### User Story 3 - Existing deployments are untouched (Priority: P1)

An organisation already signing in through their own identity provider, with no
Hub involvement, sees no change whatsoever.

**Why this priority**: Equal to Story 1. A gate that silently changes behaviour
for deployments that never asked for it is worse than no gate — and the failure
would be invisible until people could not sign in.

**Acceptance scenarios**:

1. **Given** no entitlement gate is configured, **When** anyone signs in,
   **Then** the behaviour is identical to before this feature existed.
2. **Given** no gate is configured, **When** an unrecognised identity signs in,
   **Then** an account is still created as it is today.

### Edge Cases

- **The operator enables the gate but never supplies the value to check
  against.** Every sign-in would fail with no visible cause. The system must
  refuse to start, or make the misconfiguration unmissable, rather than silently
  locking everyone out.
- **The person is the platform administrator.** Central role management must
  never be able to remove the platform administrator's own access, or a change
  made elsewhere could lock an operator out of their own instance permanently.
- **The identity carries no role information at all**, while carrying a valid
  entitlement. They are admitted as an ordinary user; absence of a role marker
  is not treated as an error.
- **Local sign-in remains available.** Enabling the gate must not remove the
  operator's non-federated way in, or a misconfiguration becomes unrecoverable.
- **The refusal page is reached by someone who is not signed in.** It must not
  disclose why they were refused in a way that describes how the organisation's
  identity provider is configured.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST verify, on every federated sign-in, that the
  presented identity carries an entitlement for this specific instance, when and
  only when an operator has configured which instance this is. Entitlement is
  established by the **presence** of the instance's own claim; its absence means
  the person is not entitled here.
- **FR-002**: The system MUST perform that check before any account is created,
  such that a refused sign-in leaves no account behind. Creating an account and
  removing it afterwards does not satisfy this requirement.
- **FR-003**: The system MUST apply the check to every sign-in, including people
  who already have an account.
- **FR-004**: The system MUST, for an entitled person, set their access level
  from the roles listed **within that same instance claim** — administrator when
  the configured administrator marker appears in the list, ordinary user
  otherwise. The marker MUST be matched exactly, including letter case.
- **FR-004a**: The system MUST NOT read the person's wider organisational group
  membership, which the identity also carries. It is broad personal data that
  answers a different question.
- **FR-005**: The system MUST remove administrator access when the identity
  stops asserting it, so that central role management remains authoritative.
- **FR-006**: The system MUST never remove administrator access from the
  platform administrator, regardless of what the identity asserts.
- **FR-007**: The system MUST present a refusal as a readable page, not as a raw
  technical error. This extends to any federated sign-in that does not complete,
  including one the person abandons at the identity provider, and MUST
  distinguish a refusal from a cancellation — the first says the organisation
  has not granted them access, the second says they changed their mind.
- **FR-008**: The refusal shown to the person MUST NOT disclose the criteria
  used, while the reason MUST be recorded where an operator can read it.
- **FR-009**: The system MUST record every refused sign-in **for a person who
  already has an account** durably, identifying who was refused and when.
- **FR-009a**: Where the refused person has no account, the system MUST still
  record the refusal somewhere an operator can read, though it need not be
  durable or queryable. This is a deliberate narrowing: the refusals worth
  investigating are those against accounts that exist — someone who had access
  and lost it, or someone probing a real account — and a refusal can only follow
  a successful authentication at the identity provider, so an unknown address is
  routine rather than hostile.
- **FR-010**: The system MUST behave exactly as it does today when no
  administrator marker is configured. Enabling the gate MUST be a deliberate
  act: an instance that already carries an identifier for unrelated reasons MUST
  NOT acquire a sign-in gate as a side effect of having one.
- **FR-011**: The system MUST fail loudly and early when the gate is enabled but
  cannot be evaluated, rather than refusing every sign-in silently.
- **FR-012**: The system MUST keep the non-federated sign-in route available so
  a misconfigured gate cannot permanently lock out an operator.

### Key Entities

- **Per-instance entitlement claim**: the Hub emits one claim per XTM One
  instance a person may use, named after that instance. Its presence *is* the
  entitlement — there is nothing to compare a value against. Its value is the
  list of roles that person holds on that instance.
- **Role marker**: an entry within the per-instance claim's list. The configured
  administrator marker being present means administrator; anything else, or an
  empty list, means ordinary user.
- **Corporate group claim**: the Hub also emits the person's full organisational
  group membership. This is deliberately **not** read — it is broad personal data
  serving a different purpose, and the per-instance claim already answers the
  question.
- **Refusal record**: an account of a sign-in that was declined — who, when, and
  why. Durable and queryable when the refused person has an account to attribute
  it to; otherwise recorded where an operator can read it, but not stored.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An unentitled person signing in creates **zero** new accounts,
  verified by comparing the account count before and after.
- **SC-002**: A refused person is shown a readable explanation and a way
  forward, with **no** raw technical error reaching them.
- **SC-003**: Every refusal against an existing account is recoverable from the
  operator's durable records afterwards, and no refusal of any kind passes
  silently.
- **SC-004**: A deployment that has not configured the gate admits and refuses
  exactly the same people as before, and provisions them the same way,
  demonstrated by its existing sign-in tests passing unchanged.

  One deliberate exception, applying to every federated deployment rather than
  only gated ones: a sign-in that **fails** now returns to the sign-in page with
  a readable message instead of a raw technical error. Abandoning the identity
  provider's consent screen previously produced a validation error about a
  missing parameter, which is not something to show a person who simply changed
  their mind. Nothing about *who* may sign in changes.
- **SC-005**: An administrator's access can be granted and withdrawn entirely
  from the central identity, with no action taken inside XTM One.
- **SC-006**: The platform administrator retains access in **every** case,
  including when the central identity asserts they should not.
- **SC-007**: A misconfigured gate is apparent to an operator at start-up rather
  than discovered through a user reporting they cannot sign in.

## Assumptions

- **Federated sign-in is the enforced route** for deployments that enable this,
  so refusing an existing account holder is intended rather than disruptive.
- **Absence of a role marker means ordinary user**, not an error — the safe
  reading, and it keeps a role-less identity usable.
- **The administrator marker is a single configured name**, matched exactly.
  This follows from the requirement that matching is case-sensitive; a list or
  pattern would be a different, larger requirement.
- **The instance is identified by the platform identifier it already has**, and
  that name selects which claim to read. Confirmed against a live Hub token on
  2026-08-19: the Hub emits a separate claim per instance, so no value
  comparison is involved.
- **Enabling the gate is a separate act from having an identifier.** The
  administrator marker is the switch, because an identifier is set for reasons
  that predate this feature entirely.
- **The claim reaches us in the profile response** rather than only in the
  sign-in token, so no additional token handling is required. Worth
  re-confirming if the Hub ever namespaces these claims, which would change
  where they can appear.
- **Only the federated route needs gating** — the alternative federation
  protocol is out of scope because the Hub does not offer it.
- **Refusal is silent to the person and verbose to the operator**, following the
  usual convention for authentication failures.
- **Durability follows attribution.** A refusal is stored durably only when
  there is an account to attach it to, which avoids widening a shared record
  store to admit rows belonging to nobody. Chosen over a dedicated store for
  refusals, which would split the operator's audit trail across two places for
  one event type.

## Out of Scope

- The second federation protocol (SAML). The Hub provides OIDC only.
- Group membership synchronisation. Only the administrator/ordinary distinction
  is read from the identity; wider group mapping is unchanged.
- Any change to how non-federated accounts are created or managed.
