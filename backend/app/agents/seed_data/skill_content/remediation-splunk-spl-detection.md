# Splunk SPL Detection — Platform Reference

## Rule Types & Data Sources

| Rule Type | Data Sources | Best For |
|-----------|-------------|----------|
| Process Monitoring | Windows Event Logs (4688), Sysmon (1) | Process execution, commands |
| File Monitoring | Windows Event Logs (4663), Sysmon (11) | File drops, persistence |
| Network Monitoring | Firewall Logs, Sysmon (3), Windows (5156) | C2, exfiltration |
| Authentication Monitoring | Windows (4624, 4625, 4767, 4768) | Privilege escalation, lateral movement |
| Registry Monitoring | Windows (4657), Sysmon (13, 14) | Persistence, config changes |

## SPL Best Practices

- Use `index=*` for cross-index searches
- Prefer `EventCode=4688` over Sysmon EventID 1 — NEVER mix
- Use `stats`, `count`, `timechart` for aggregation
- Proper time ranges with `earliest` and `latest`
- Filter early, aggregate late
- No comments, no markdown, no backticks in output
- Use `<br />` for line breaks between pipeline stages

## Example Patterns

```
index=windows sourcetype="WinEventLog:Security" EventCode=4688 \\
CommandLine="*suspicious*"
```

```
index=sysmon sourcetype="XmlWinEventLog:Microsoft-Windows-Sysmon/Operational" \\
EventID=11 TargetFilename="*suspicious*"
```
