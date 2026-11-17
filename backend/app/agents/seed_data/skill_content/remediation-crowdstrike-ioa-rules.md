# CrowdStrike Custom IOA — Platform Reference

## Rule Types

| Rule Type | Key Fields | Best For |
|-----------|-----------|----------|
| Process Creation | ImageFileName, CommandLine, Parent/Grandparent | Process execution, tool launches |
| File Creation | Same + File Path | File drops, persistence, staging |
| Network Connection | Same + Remote IP, Port, Connection Type, Domain | C2, exfiltration |
| Domain Name | ImageFileName, CommandLine, Domain Name | Malicious domains, DNS queries |

## Actions & Severity

Actions: Detect, Monitor, Block Execution, Kill Process
Severity: Critical, High, Medium, Low, Informational

## Regex Syntax

- Case insensitive by default — NO manual case patterns
- `.*` for wildcards, `\\.` for dots, `\\\\` for backslashes
- SIMPLE patterns: `.*powershell\\.exe` not `[pP][oO][wW]...`
- Keywords: `.*download.*`, `.*invoke-webrequest.*`
- Paths: `.*\\\\temp\\\\.*`, `.*\\\\programdata\\\\.*`

## Field Rules

- Core 6 fields (grandparent/parent/image filename + command line): \
use `.*` when not specified (never null)
- `file_path`: null unless File Creation
- `remote_ip_address`, `remote_port`, `connection_type`: null unless \
Network Connection
- `domain_name`: null unless Domain Name
