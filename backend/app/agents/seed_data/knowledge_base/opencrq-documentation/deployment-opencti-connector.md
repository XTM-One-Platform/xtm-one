# 🔗 OpenCTI Connector

This page explains how to set up the OpenCTI Live Stream the OpenCRQ connector consumes: which objects the stream must carry, how to filter it, and what the token needs to be able to read. See [Configuration](configuration.md#opencti-live-stream-import-of-stix-bundles) for the per-tenant `CONNECTORS__OPENCTI__<tenant-uuid>__URL`/`CONNECTORS__OPENCTI__<tenant-uuid>__TOKEN` variables.

## What the connector reads

The connector subscribes to an OpenCTI Live Stream (Server-Sent Events) and keeps threat intelligence (`threat_analyses`) up to date, as an alternative to manually uploading STIX bundle files. It needs four kinds of object, and reads two more when the stream carries them:

- **Threats**: intrusion sets, threat actors (groups and individuals) and campaigns.
- **`targets` and `uses` relationships** carried by those threats: the claims the platform stores and scores.
- **`attributed-to` relationships** between two threats, such as a campaign attributed to an intrusion set. An `attributed-to` toward anything else, an identity for instance, is ignored.
- **Identities** named as producers by those relationships through `created_by_ref`: how a claim gets attributed to who made it.
- **Malware** a threat `uses`: its name and types.
- **Reports** citing a threat in their `object_refs`: title, labels, publication date and the start of the description. A report citing no threat is ignored.

OpenCRQ's internal model reads the last two, with the threat's own description, motivations, goals and claims, to give each threat a probability per attack outcome. A stream without them still delivers, and the model then assesses each threat from less.

OpenCTI exports a threat actor group and an individual threat actor both as a STIX `threat-actor`, and the platform stores both as one threat type. Every threat follows OpenCTI live: created, edited, renamed, merged or deleted there, it changes the same way here.

## Stream filter

**The filter has to admit the relationships, not just the actors.** The connector requests the stream with `no-dependencies=true`, so OpenCTI sends what the filter selects and nothing else. Without that flag the server publishes the entire dependency closure of every element it matches under that element's own event id, and a single well connected intrusion set can hold the catch-up for hours while almost nothing the connector reads arrives.

A filter must therefore admit, for each threat type it admits (`Intrusion-Set`, `Threat-Actor-Group`, `Threat-Actor-Individual`, their parent `Threat-Actor`, or `Campaign`):

- `uses` from that type to an `Attack-Pattern`,
- `targets` from that type to a `Location`, a `Sector` and a `Vulnerability`.

To feed the model, it can also admit `uses` from that type to a `Malware`, and `Report` entities. The verdict below does not judge these two. The stream OpenCRQ creates by itself admits both.

The filter below is the one OpenCRQ applies to the stream it manages (`CANONICAL_STREAM_FILTER` in `back/src/features/deployment-integrations/opencti-managed-stream.ts`); keep the two in step and bump `MANAGED_STREAM_FILTER_VERSION` when it changes, so every managed stream is updated at the next start.

When it admits two threat types or more, it must also admit `attributed-to` between them: from a campaign to an intrusion set or a threat actor, and from an intrusion set to a threat actor. A filter admitting intrusion sets alone, with their relationships, still delivers.

Referenced objects are still resolved outside that flag, so an admitted relationship arrives with the object at each end and with the identity named as its producer. Only what the filter rejects outright is lost.

The filter is a group of alternatives, each one an `and` of clauses, and OpenCTI's filter editor cannot build nested groups. Paste it as JSON instead:

```json
{
  "mode": "or",
  "filters": [],
  "filterGroups": [
    {
      "mode": "and",
      "filters": [
        { "key": ["entity_type"], "operator": "eq", "values": ["Intrusion-Set", "Threat-Actor-Group", "Threat-Actor-Individual", "Campaign", "Report"], "mode": "or" }
      ],
      "filterGroups": []
    },
    {
      "mode": "and",
      "filters": [
        { "key": ["entity_type"], "operator": "eq", "values": ["targets"], "mode": "or" },
        { "key": ["fromTypes"], "operator": "eq", "values": ["Intrusion-Set", "Threat-Actor-Group", "Threat-Actor-Individual", "Campaign"], "mode": "or" },
        { "key": ["toTypes"], "operator": "eq", "values": ["Location", "Sector", "Vulnerability"], "mode": "or" }
      ],
      "filterGroups": []
    },
    {
      "mode": "and",
      "filters": [
        { "key": ["entity_type"], "operator": "eq", "values": ["uses"], "mode": "or" },
        { "key": ["fromTypes"], "operator": "eq", "values": ["Intrusion-Set", "Threat-Actor-Group", "Threat-Actor-Individual", "Campaign"], "mode": "or" },
        { "key": ["toTypes"], "operator": "eq", "values": ["Attack-Pattern", "Malware"], "mode": "or" }
      ],
      "filterGroups": []
    },
    {
      "mode": "and",
      "filters": [
        { "key": ["entity_type"], "operator": "eq", "values": ["attributed-to"], "mode": "or" },
        { "key": ["fromTypes"], "operator": "eq", "values": ["Campaign", "Intrusion-Set"], "mode": "or" },
        { "key": ["toTypes"], "operator": "eq", "values": ["Intrusion-Set", "Threat-Actor-Group", "Threat-Actor-Individual"], "mode": "or" }
      ],
      "filterGroups": []
    }
  ]
}
```

To bring intrusion sets alone, keep only `Intrusion-Set` in the three type lists and drop the `attributed-to` group.

Note that `relationship_type` is not a key the stream filter language accepts: a relationship is named by `entity_type` (`targets`, `uses`, `attributed-to`) and narrowed by `fromTypes` / `toTypes`.

**A stream filtered on the threats alone no longer works.** Earlier versions relied on the dependency fan-out to bring the relationships in behind an entities-only filter. That stream is now reported as *Incompatible filter* and has to be widened, or replaced, before it delivers anything but bare threats.

**The verdict in the UI.** Integrations → OpenCTI judges each stream before one is picked, by asking whether the filter admits each object the connector reads. *Delivers threat actors* means it admits all of them. *Incompatible filter* means it admits threats but leaves out at least one of the relationships above; the notice names which ones, from which threat type, and what each one costs. *No threat actors* means it provably admits no threat type at all. *Stopped in OpenCTI* means the stream refuses connections until someone restarts it there. *Cannot be judged* means the filter uses conditions the platform does not evaluate, and the filter is shown beside it so you can judge it yourself. A warned stream stays selectable: the verdict is advisory, never a block.

A stream carrying an `origin_filters` is reported separately as replaying no history, because OpenCTI skips its replay entirely: only what it publishes from the connection onwards arrives.

## Inferred relationships

The connector always requests the stream with `with-inferences=true` — there is no per-tenant or per-deployment switch to turn this off. If OpenCTI's inference rule engine is disabled, this has no effect. If it is enabled, relationships produced by rule inference enter the stream alongside asserted ones, which grows both the stream's volume and the number of claim rows stored per actor. The tenant's confidence gate (Threat Relevance settings) is what actually decides which of those claims are read, not this stream option.

## Connecting a tenant

Connecting a tenant is automatic: set `CONNECTORS__OPENCTI__<tenant-uuid>__URL` and `__TOKEN` and start OpenCRQ. At each start it creates the Live Stream it manages ("OpenCRQ threat feed", with the filter above), or updates its filter when it is out of date, and binds a tenant that has nothing bound under the "OpenCTI (deployment)" service account. A restart reuses the same stream: it is recognised by the marker in its description, not by its name.

- **Grant the token the stream capability** — the user behind the token needs `TAXIIAPI_SETCOLLECTIONS` (Manage CTI data sharing > Manage Live Streams), or `BYPASS`. Without it OpenCRQ writes nothing in OpenCTI and the connector page names the missing capability; grant it and restart, or create a stream by hand and pick it with **Configure**.
- **Check the token's marking access** — the user behind that tenant's `CONNECTORS__OPENCTI__<tenant-uuid>__TOKEN` must be able to read every marking definition applied to the threats, relationships and producer identities in scope. OpenCTI drops a relation from the stream entirely when its source or target cannot be loaded for the stream user — a marking access gap silently narrows the feed the same way a wrong filter does, and the platform cannot tell the two apart. Every object mirrored keeps its markings, and each OpenCRQ user reads only what their own OpenCTI markings allow.
- **Pick another stream if you want** — **Configure** on the OpenCTI connector binds any stream; an admin's binding is never replaced at start, and the verdict above still judges it.
