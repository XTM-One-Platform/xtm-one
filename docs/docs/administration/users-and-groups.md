# Users and groups

`Users` and `Groups`, in the administration menu, are where administrators manage accounts and the groups that share resources, set limits and decide AI access. Both pages are for administrators only.

## The Users page

`Users` lists every account with its role, a `Platform Admin` or `Disabled` badge where it applies, and how many integrations, MCP servers, agents and knowledge bases it owns. Search by name or email, or filter by group.

Open a user to see their page. Its tabs are:

- `Profile`: name, email, role, status, sign-in method, two-factor authentication and password.
- `Groups`: the groups the user belongs to.
- `Objects`: the agents, knowledge bases, MCP servers, custom tools and integrations the user owns.
- `API Keys`: the user's personal API keys, with `Revoke` (see [API keys](settings.md#api-keys)).
- `Quotas`: the user's own limits and their usage.
- `Activity`: the audit log entries of the user's actions, with a search box and `Action` and `Entity` filters.

## Add a user

There are two ways to give someone an account.

`New User` creates the account at once. Choose the `Account Type`:

- `Local Account`: you set the email, the full name and a password, and pass the password on to the person.
- `SSO Pre-provision`: the account is created without a password. The first time the person signs in through single sign-on, the account is linked to them by email, with the role and profile you set. This type needs single sign-on to be turned on first.

Turn on `Administrator` to make the account an administrator.

`Invite user` emails the person a single-use link to create their own account, with the email address and the role you set. The link works once and expires after 7 days by default (see [Admin-managed accounts](../deployment/authentication.md#admin-managed-accounts)). An invitation needs the platform to send email: without a mail server configured, the invitation is refused with an error that says so (see [Password reset and email](../deployment/authentication.md#password-reset-and-email)).

The same dialog lists the `Pending invitations`, with their role and expiry. Revoke one to cancel it, for example to send it again: an email address can have only one pending invitation, and an address that already has an account cannot be invited.

## Roles

An account is either an administrator or a user. `Promote` and `Demote`, at the top of the user's page, switch the role. The change applies on the person's next request.

The bootstrap administrator, the account set by `ADMIN_EMAIL` in the deployment, carries the `Platform Admin` badge. It cannot be demoted, disabled or deleted, its email cannot be changed nor given to another account, and every restart makes it an administrator again (see [Local accounts](../deployment/authentication.md#local-accounts)).

When the platform has no account yet, the first account registered becomes an administrator.

## Disable, enable or delete an account

`Disable` stops the person from signing in and keeps everything they own. While the account is disabled, the assignments and flows it owns are paused, shared ones included; the company-managed ones it created keep running. What was shared with the person stays shared, so `Enable` gives them back the access they had.

`Delete` removes the account for good and cannot be undone:

- the company-managed resources the person owned (agents, assignments, integrations, knowledge bases, MCP servers, custom tools, channel bots, prompts, variables) are handed to you, so they keep working for everyone;
- their private resources are deleted with the account: agents, assignments and their runs, integrations, knowledge bases, MCP servers, custom tools, conversations, API keys and the rest;
- their access to resources shared with them is removed, and their memory is cleared;
- the audit log entries of their own actions go with the account. The entry of the deletion, written under your name, stays.

You cannot disable or delete your own account. Prefer `Disable` when you may need the account or its history again.

## Email address

People cannot change their own email address: the other Filigran platforms (OpenCTI, OpenAEV, OpenCRQ) find a person's account by it. The `Email` card of the user's page changes it, for local and SSO accounts alike, to an address no other account has and, when `Allowed Domains` is set, in one of those domains. The change applies at once:

- when the platform sends email, the old and the new address are both told of the change, without a link;
- password reset links already sent to the old address stop working;
- the audit log records the old and the new address.

## Passwords

The `Password` card of the user's page offers two ways to change a local password:

- `Email a reset link` sends the person a single-use link to choose their own password: you never see it. The link expires after 24 hours by default. It needs the platform to send email: without a mail server, the request is refused with an error.
- `Set a password directly` sets the password you type, which you then pass on to the person. It works without email.

For an account that signs in only through single sign-on, neither is available: the identity provider manages the password. For an account that has both, the change only affects its local password; single sign-on is unaffected.

## Two-factor authentication

The `Two-factor authentication` card shows whether the person has enrolled an authenticator app, and when. Nobody can read their secret or recovery codes, administrators included.

If the person has lost their device and their recovery codes, `Reset two-factor` removes the enrollment: they sign in with their password alone until they enroll a new device, and their old authenticator and recovery codes stop working at once. Only the person can turn two-factor authentication on, from their own profile. Each reset is recorded in the audit log. See [Two-factor authentication](../deployment/authentication.md#two-factor-authentication-totp).

## Impersonate a user

`Impersonate` lets you use the platform as the person sees it, to reproduce a problem they report. It is offered for an active account other than your own, and not while you are already impersonating someone.

While you impersonate, a banner at the top of every page names the person and you, with `Stop Impersonating` to return to your own account. The impersonation ends when your own session ends.

What to know before you start:

- Starting an impersonation is recorded in the audit log. What you then do is done as the person, and is recorded under their name.
- Some actions are refused while you impersonate: creating, changing or deleting an API key, signing in to another product through XTM One, and signing in XTM One Desktop.
- You have the person's access, not yours: what they cannot see, you cannot see either.

## Quotas for one user

The `Quotas` tab sets limits for this person alone, above their groups' limits and the platform default. For agentic executions (chat messages and assignment runs), set a limit per period, turn on `Unlimited` to lift it, or clear it to inherit from the person's groups or the platform. The platform-wide quota still applies as a hard cap, and a limit cannot exceed what the license allows.

The tab also shows the person's current usage. `Reset usage` sets their counters back to zero, and is recorded in the audit log. See [Quotas](settings.md#quotas) for how the limits combine.

Quotas need an activated license or a registered Enterprise Edition platform.

## Groups

`Groups` lists every group with its member count. `New Group` creates one with a name and an optional description.

Groups have two uses:

- **Sharing.** A resource shared with a group is shared with its current members. Adding someone to the group gives them access; removing them takes it away.
- **Settings for a team.** A group can carry quotas and an AI switch, and can be reserved an LLM provider or kept off one (see [Reserving a provider for teams](settings.md#reserving-a-provider-for-teams) and [Keeping people off a provider](settings.md#keeping-people-off-a-provider)).

With SAML, groups can also be created and filled from the identity provider at every sign-in (see [Group mapping](../deployment/authentication.md#group-mapping)). A group with [Auto join](#auto-join) on takes in everyone who signs in through single sign-on.

A group's page has four tabs:

- `Overview`: its name, description and `Kind`, with the `AI access` and `Auto join` cards. The kind (`Org unit`, `Access list`, `Distribution list`, or `Not classified`) tells LLM Analytics which groups are org units.
- `Members`: add members with `Add Member`, remove them, and search or filter by role. From a user's page, the `Groups` tab does the same with `Add to Group`.
- `Managers`: the people who follow the AI usage of the group's members. See [Group managers](#group-managers).
- `Quotas`: the limit each member gets. A user's own quota wins over the group's; with no group limit, members inherit the platform default.

Deleting a group removes its members' access to what was shared with the group. A group that an LLM provider excludes cannot be deleted: remove it from the provider's exclusions in `Settings` > `AI Models` first.

## Group managers

A manager follows the AI usage of the group's current members in Model usage (under `Administration > Observability`), and through an agent with the platform insights tools: the agents they use, the costs and the errors. A manager does not have to be a member of the group, and gets no other right. See [Group managers](../deployment/monitoring.md#group-managers).

## Switching AI off for a group

The `AI access` card's `AI disabled for this group` switch turns AI off for every member of the group, the way [Running without AI](running-without-ai.md) does for the whole platform. It suits a team that policy keeps off models (a works council, a regulated department) while the others keep using AI.

For a member, as long as the group has it off:

- chat, agents, assistants and every endpoint that answers with a model refuse them with the platform's own `ai_disabled` refusal, and the AI areas of the navigation are marked unavailable;
- the assignments, flows and delegated tasks that run **as them** (the ones they own) are held, as they are while AI is switched off platform-wide, and run by themselves once AI is back; triggering one by hand is refused;
- no model, embedding or image provider is used for their content, whichever providers they were granted.

A person in several groups is cut off as soon as one of them has AI off. An administrator is held to it like everyone else: nothing an administrator needs to undo it (the groups page, the settings) uses a model. Everything that does not need a model keeps working, the same list as [Running without AI](running-without-ai.md#what-keeps-working).

Members read the change within seconds; a replica that did not serve it follows within five. Switching it on or off is recorded in the audit log, with the previous value. The platform-wide switch wins: with AI off for the platform, a group's setting changes nothing.

## Auto join

The `Auto join` card's `Single sign-on users join this group` switch (default: off) adds everyone who signs in through single sign-on, OIDC or SAML, to the group at every sign-in: an account created at its first sign-in, an account an administrator created for a single sign-on user, and an existing account alike. The `Groups` list marks such a group with an `Auto join` badge. It is the way to give every single sign-on user a group's shares, quotas, AI access or provider reservation without adding each one by hand.

- **Once per person.** Someone already in the group, however they joined, is left as is. Each membership it adds is recorded in the audit log, with the sign-in as the actor.
- **Only single sign-on.** Local accounts, accounts provisioned through SCIM and accounts created through the API join only when they sign in through single sign-on.
- **With SAML group mapping.** The mapping manages only the memberships it created, so it never removes one that auto join added, even when the assertion does not name the group.
- **Removing someone.** An administrator can remove a member from the `Members` tab; while the switch stays on, the next single sign-on adds them again.
- **CTEM Viewers.** The group XTM One seeds for CTEM read access has the switch on. Every account, however it is created, joins it once, at its creation; single sign-on does not add anyone to it again, so a person an administrator removes stays removed. Switching it off keeps it off.

Switching it on adds nobody at once: each person joins at their next single sign-on. Switching it off removes nobody. Both are recorded in the audit log with the previous value.

## Terms of Use

The Terms of Use are accepted once for the whole platform, by an administrator, not by each user: there is no per-user acceptance to follow here. See [Terms of Use acceptance](../deployment/authentication.md#terms-of-use-acceptance).

## Next step

[Organization profile](organization.md) describes your organization for every agent.
