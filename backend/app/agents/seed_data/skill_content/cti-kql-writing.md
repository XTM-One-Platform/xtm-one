# KQL Query Writing

KQL (Kusto Query Language) is used in Microsoft Sentinel, Microsoft Defender, and Azure Data Explorer for querying security logs and building detection rules. Use this skill when the user asks for a KQL query, a Microsoft Sentinel / Defender / Azure Log Analytics detection or hunt, or wants to translate a finding from a hash investigation or malware analysis into KQL.

Do not confuse it with Elastic's "KQL" (Kibana Query Language), a different and much simpler filter syntax. For Elastic, write ES|QL or EQL, or convert a Sigma rule.

## Workflow

1. **Ground the behaviour** in a technique: with the MITRE ATT&CK integration, `search_mitre_attack` and `get_mitre_attack_object` to confirm the technique ID and its data sources.
2. **Check existing coverage**: with the Sentinel integration, `list_sentinel_analytics_rules` (is the technique already covered?) and `list_sentinel_data_connectors` (are the tables you need actually ingested?).
3. **Write the query** using the syntax, patterns and best practices below.
4. **Run it read-only** on a short window first:
   - With the Sentinel integration: `run_sentinel_kql_query` (Log Analytics workspace; time column `TimeGenerated`).
   - With the Defender integration: `run_defender_hunting_query` (Defender XDR advanced hunting; the time column there is `Timestamp`, and advanced hunting keeps about 30 days of data).
5. **Tune** on the results: add exclusions for recurring benign hits, check volume, then widen the window.
6. **Deploy or store only when asked.** Query text goes in the answer. Do not create analytics rules, push threat indicators (`create_sentinel_threat_indicator`, `create_defender_indicator`) or create an OpenCTI Indicator (`create_opencti_indicator` with `pattern_type` kql) unless the user or the assignment asks for it. For conversion of a STIX pattern or Sigma rule into KQL, load the built-in **OpenCTI Detection Rule Converter Skill** if it is attached.

Use only the tools you actually have; when an integration is not connected, say which test was skipped and mark the query "not executed" instead of guessing its result.

### Useful context lookups

- Threat-intel indicators already in Sentinel: `query_sentinel_threat_indicators`, `list_sentinel_threat_indicators` (to join against `ThreatIntelligenceIndicator` instead of hard-coding IOCs).
- Watchlists for allow-lists and VIP accounts: `list_sentinel_watchlists`, `get_sentinel_watchlist_items` (reference them with `_GetWatchlist('ALIAS')`).
- Entity context for hits: `get_defender_machine`, `get_defender_file_info`, `get_defender_ip_stats`, `get_defender_domain_stats`.

## Core Syntax

### Table references
```kql
SecurityEvent                        // Windows Security Events
DeviceProcessEvents                  // Defender for Endpoint
DeviceNetworkEvents                  // Network connections
DeviceFileEvents                     // File operations
EmailEvents                          // Defender for Office 365
SigninLogs                           // Azure AD (Entra ID) sign-ins
AuditLogs                            // Azure AD (Entra ID) audit
CommonSecurityLog                    // CEF/Syslog
ThreatIntelligenceIndicator          // TI feed indicators
```

### Operators
```kql
| where TimeGenerated > ago(24h)     // Time filter
| where EventID == 4688              // Exact match
| where ProcessCommandLine contains "-enc"  // Substring
| where ProcessCommandLine matches regex @".*-e(nc)?.*"  // Regex
| where SourceIP !in ("10.0.0.1", "10.0.0.2")  // Not in list
| where isnotempty(AccountName)      // Not null/empty
| extend NewColumn = extract(@"pattern", 1, SourceField)  // Extract
| project TimeGenerated, Account, Computer  // Select columns
| summarize count() by bin(TimeGenerated, 1h), Account  // Aggregate
| sort by TimeGenerated desc         // Sort
| take 100                           // Limit results
| join kind=inner (OtherTable) on CommonField  // Join
```

### String operators
| Operator | Description | Case-sensitive |
|----------|-------------|---------------|
| `==` | Exact match | Yes |
| `=~` | Exact match | No |
| `contains` | Substring | No |
| `contains_cs` | Substring | Yes |
| `startswith` | Starts with | No |
| `endswith` | Ends with | No |
| `matches regex` | Regex match | Yes |
| `has` | Word boundary match | No |
| `in` | In list | Yes |
| `in~` | In list | No |

## Common Detection Patterns

The examples use `TimeGenerated` (Sentinel). For Defender advanced hunting, replace it with `Timestamp`.

### Suspicious process execution (T1059)
```kql
DeviceProcessEvents
| where TimeGenerated > ago(24h)
| where FileName in~ ("powershell.exe", "cmd.exe", "wscript.exe", "cscript.exe")
| where ProcessCommandLine contains_cs "-enc"
    or ProcessCommandLine contains "bypass"
    or ProcessCommandLine contains "downloadstring"
| project TimeGenerated, DeviceName, AccountName, FileName, ProcessCommandLine, InitiatingProcessFileName
```

### Suspicious parent-child (T1566.001)
```kql
DeviceProcessEvents
| where TimeGenerated > ago(24h)
| where InitiatingProcessFileName in~ ("outlook.exe", "winword.exe", "excel.exe", "powerpnt.exe")
| where FileName in~ ("cmd.exe", "powershell.exe", "wscript.exe", "mshta.exe", "certutil.exe")
| project TimeGenerated, DeviceName, AccountName, InitiatingProcessFileName, FileName, ProcessCommandLine
```

### Outbound connection to IOC (C2)
```kql
let IOC_IPs = dynamic(["203.0.113.42", "198.51.100.10"]);
let IOC_Domains = dynamic(["evil.example.com", "c2.badactor.net"]);
DeviceNetworkEvents
| where TimeGenerated > ago(7d)
| where RemoteIP in (IOC_IPs) or RemoteUrl has_any (IOC_Domains)
| project TimeGenerated, DeviceName, RemoteIP, RemoteUrl, RemotePort, InitiatingProcessFileName
```

### Failed sign-in brute force (T1110)
```kql
SigninLogs
| where TimeGenerated > ago(24h)
| where ResultType != "0"
| summarize FailedAttempts = count(), DistinctAccounts = dcount(UserPrincipalName)
    by IPAddress, bin(TimeGenerated, 15m)
| where FailedAttempts > 10
| sort by FailedAttempts desc
```

### Lateral movement - PsExec (T1570)
```kql
DeviceProcessEvents
| where TimeGenerated > ago(24h)
| where FileName =~ "psexesvc.exe"
    or (FileName =~ "cmd.exe" and ProcessCommandLine contains "\\\\")
    or ProcessCommandLine contains "-accepteula -s cmd"
| project TimeGenerated, DeviceName, AccountName, FileName, ProcessCommandLine
```

### DNS query for suspicious domain (T1071.004)
```kql
DeviceEvents
| where TimeGenerated > ago(24h)
| where ActionType == "DnsQueryResponse"
| extend DnsQuery = extractjson("$.DnsQueryString", AdditionalFields)
| where DnsQuery endswith ".onion.ws"
    or DnsQuery endswith ".tor2web.io"
    or DnsQuery matches regex @"^[a-z0-9]{20,}\."
| project TimeGenerated, DeviceName, DnsQuery
```

## Sentinel Analytics Rule Format

```kql
// Rule name: [Descriptive title]
// Description: [What this detects and why]
// MITRE ATT&CK: [Technique IDs]
// Severity: High|Medium|Low|Informational
// Tactics: [InitialAccess, Execution, etc.]

// Query:
[KQL query here]
```

## Best Practices

- Always include a time filter (`ago()`) to limit query scope
- Use `has` instead of `contains` when searching for whole words (faster)
- Prefer `in~` over multiple `or` conditions for case-insensitive list matching
- Use `let` statements for IOC lists to keep queries readable
- Add `project` to select only needed columns (reduces result size)
- Test queries on small time windows first before expanding
- Include comments explaining detection logic
- Treat an empty result on a table that is not ingested as "untested", not as "no activity"

## Output

Return each query as a fenced `kql` block in the answer, with the analytics-rule header above it and a one-line test status ("ran on Sentinel over 7 days: 3 hits, all benign admin activity, excluded" or "not executed: no Sentinel or Defender integration connected"). Suggested file name when the user wants a file (`generate_file`): `TECHNIQUE_ID-SLUG.kql`.

## Related skills

- **Sigma Rule Writing** - vendor-agnostic version of the same detection.
- **Hash Investigation**, **Malware Analysis** - produce the behaviour the query detects.
