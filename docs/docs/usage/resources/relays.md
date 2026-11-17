# Relays

A **relay** lets XTM One reach the tools that live inside a network it cannot
reach directly - a Splunk in your data center, a Tanium server, an internal API,
an on-premises OpenCTI - without opening a single inbound flow toward that
network.

You install the relay on a machine or in a container inside the network. It
connects **out** to XTM One over HTTPS and pulls the requests it should carry.
When an integration, an MCP server or a flow step is routed through it, its
calls leave from that network, through the relay, instead of leaving from the
platform. Nothing listens for inbound connections, and there is no VPN to set
up.

You find them under `Tools & Integrations` > `Relays`. A platform
administrator can turn relays off entirely with `RELAYS_ENABLED=false` (see
[Configuration](../../deployment/configuration.md#relays)).

## Relay groups

Relays belong to a **relay group**, and a group stands for one network: a data
center, a cloud account, a site, an OT zone. You route a resource through a
group, never through one particular relay: every relay of the group can carry
its requests, and the platform balances them across the relays that are online.
Two or more relays in a group give you redundancy - when one stops, the others
keep carrying the traffic.

Only administrators create relay groups. A group is either available to every
user (company-managed) or shared with specific groups and people, like any other
resource (see [Sharing and access](sharing-and-access.md)). Once a group is
shared with you, you can route your own integrations through it.

To create one:

1. Open `Relays` and select `New relay group`.
2. Give it a name (for example `Paris datacenter`), an optional zone and a
   description of what the network holds.
3. Optionally list the **allowed destinations** (`host:port`, `*.corp.local:443`,
   `10.20.0.0/16:8089`). The platform then refuses any request to anything else
   before it reaches a relay. Each relay also enforces its own list, which always
   has the last word.
4. Select `Create and enroll a relay`.

## Enroll a relay

`Enroll a relay` (on the group's page) walks you through the install:

1. Choose where the relay runs: `Linux`, `Windows`, `macOS`, `Docker` or
   `Kubernetes`.
2. Optionally name it (the machine's hostname is used otherwise) and set the
   destinations it may reach. With an empty list the relay reaches nothing.
3. Select `Generate the install command` and run the command it shows inside the
   network:
    - on Linux and macOS, as root - it downloads the relay from your XTM One
      instance, checks its signature, installs it as a system service and
      enrolls it (on Linux it needs glibc 2.35 or newer: Ubuntu 22.04, Debian
      12 or a later distribution);
    - on Windows, in an elevated PowerShell - same steps, as a Windows service;
    - with Docker, on a host inside the network;
    - with Kubernetes, with `kubectl` against a cluster inside the network - each
      replica enrolls as its own relay.
4. The dialog waits until the relay connects, then tells you it is online.

The enrollment key in the command is shown only once. A key for one machine
enrolls one relay and expires soon; a key for several replicas (Kubernetes)
stays valid for 7 days.

Every instance serves the relay installers and updates itself, matching its own
version, so an air-gapped deployment needs nothing else. A relay installed as a
service updates itself; a relay in a container is updated by pulling a newer
image.

## Route a resource through a relay group

On the configuration of an integration, a remote MCP server, or a flow
`HTTP request` step, choose the group under `Route through`. `Direct (from the
platform)` keeps the usual behavior.

Once a resource is routed, every request it makes - its tools, its connectivity
test, its OAuth sign-in and token refresh, its event polling - goes through the
group. When no relay of the group is online, those requests fail with a clear
message: a routed resource **never** falls back to a direct call.

!!! note "Local MCP servers"
    A `stdio` MCP server runs on the platform itself, so only a remote MCP server
    can be routed through a relay group.

## Follow a group

The group's page has six tabs:

- `Overview` - relays online, requests and failures over the last 24 hours,
  average latency, traffic, and the destination policy.
- `Relays` - each relay with its status, version, connection type (WebSocket or
  long-poll), what its own list lets it reach and when it was last seen. From
  here you revoke or delete a relay, and manage the enrollment keys.
- `Routed resources` - the integrations, MCP servers and flows routed through
  the group.
- `Activity` - every request a relay carried: when, which relay, the
  destination host, the method, the outcome, the size and the duration. Only
  this metadata is kept - never a header, a body or a credential.
- `Diagnostics` - send one test request to a destination from the group's
  network, and the commands to run on the relay's machine when something does
  not connect.
- `Settings` - the group's name, zone, allowed destinations and sharing, and
  deleting it. A group that something is still routed through cannot be
  deleted: route those resources elsewhere first.

## Configure a relay

A relay reads a few settings from `relay.toml`:

- `/etc/xtm-one-relay/relay.toml` on Linux;
- `/Library/Application Support/XTM One Relay/relay.toml` on macOS;
- `%ProgramData%\XTM One Relay\relay.toml` on Windows.

It reloads the file when it changes. The settings you are most likely to need:

| Setting | What it does |
|---|---|
| `allowed_destinations` | The hosts, wildcards and networks the relay may reach, each with its port (`splunk.corp.local:8089`, `10.20.0.0/16:443`). Anything else is refused. `*` allows every destination - the security audit flags it. |
| `ca_bundle` | A file of internal certificate authorities to trust, for services signed by your own CA. |
| `insecure_tls_destinations` | Hosts whose certificate the relay does not check. Use it only for a lab. |
| `upstream_proxy` | An outbound proxy the relay must use to reach XTM One. |
| `transport` | `auto` (WebSocket, falling back to long-poll), `websocket` or `long_poll` - for proxies that refuse WebSocket upgrades. |
| `max_concurrent_requests` | How many requests the relay carries at once. |
| `health_listen` | An address for a local health endpoint (for example `127.0.0.1:9180`): `GET /healthz` answers 200 while the relay is connected to the platform. |

In a container, every setting is an environment variable instead
(`XTM_RELAY_ALLOWED_DESTINATIONS`, `XTM_RELAY_CA_BUNDLE`, ...).

The relay trusts the Mozilla roots built into it and the operating system's
certificate store. Keep the host's CA certificates package up to date: the
relay cannot drop a root the host still trusts.

On the relay's machine:

- `xtm-one-relay status` says whether it is enrolled and connected (exit code 0
  when online);
- `xtm-one-relay check-config` shows the configuration it runs with and its
  effective allow-list;
- `xtm-one-relay test-destination https://splunk.corp.local:8089/` sends the
  same request from the machine, through the relay's own checks.

## Security

- The relay only ever connects out. Its credential is stored on the machine,
  readable by the relay's service account only, and an administrator can revoke
  it at any time from the `Relays` tab.
- Two lists decide what a relay reaches: the group's (checked by the platform)
  and the relay's own (checked on the machine). A request must pass both.
- The relay checks the addresses it is about to connect to, after resolving the
  name, so a name that resolves somewhere unexpected is caught. It never reaches
  a cloud metadata endpoint, whatever its list says, and `*` does not cover its
  own machine or link-local addresses.
- The [security audit](../../administration/settings.md#security-audit) reports
  relays that allow every destination, relays older than the minimum supported
  version, groups in use with no relay online, and groups with a single relay.

## Next step

Route one of your [integrations](integrations.md) or
[MCP servers](mcp-servers.md) through the group you created.
