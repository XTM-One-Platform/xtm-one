# YARA Rule Writing

YARA rules identify and classify malware by matching byte patterns, strings, and conditions in files. Use this skill when the user asks for a YARA rule ("write YARA for this sample / family"), or when a hash investigation or malware analysis surfaces a sample worth a static-content rule.

## Workflow

1. **Collect the material.** Gather the sample's hashes, strings, imports, sections and behaviour:
   - With the VirusTotal integration: `get_vt_file_report` (names, size, type, imports, signatures, existing YARA matches from crowdsourced rulesets), `get_vt_file_behaviour` (sandbox behaviour, dropped files, mutexes), `get_vt_related_objects` (parents, dropped and bundled files, similar files).
   - With the abuse.ch integration: `lookup_abusech_malware_hash` and `search_abusech_malware` (MalwareBazaar samples of the same family, for a test set).
   - With the ANY.RUN integration: `get_anyrun_analysis_report`, `get_anyrun_yara_results` for a sandboxed sample.
2. **Check for existing rules** before writing a new one:
   - With the Intel 471 integration: `search_intel471_yara` (vendor YARA rules by family).
   - With the Ransomware.live integration: `get_ransomlive_yara` (YARA rules published for a ransomware group).
   - With the Recorded Future integration: `search_rf_detection_rules`.
   - With the OpenCTI integration: `search_opencti` / `list_opencti_entities` for Indicators with `pattern_type` yara linked to the family.
3. **Write the rule** following the structure, best practices and naming convention below.
4. **Check the syntax** with `code_interpreter`: if the `yara` Python module is available in the sandbox, compile the rule and run it against any test bytes you have; otherwise do a structural lint (balanced braces, every string referenced in the condition is defined, hex strings well formed) and say the rule was not compiled.
5. **Validate against a corpus** (see "Validation against a real corpus").
6. **Store only when asked.** Rule text goes in the answer. Create an OpenCTI Indicator (`create_opencti_indicator` with `pattern_type` yara) only when the user or the assignment asks for it.

Use only the tools you actually have; when an integration is not connected, say which source or test was skipped instead of guessing its result.

## Rule Structure

```yara
rule RuleName : tag1 tag2 {
    meta:
        author = "CTI Platform"
        date = "YYYY-MM-DD"
        description = "Description of what this rule detects"
        reference = "https://source-reference.com"
        tlp = "GREEN"
        mitre_attack = "T1566"
        hash = "sha256_of_sample"
        confidence = "high"

    strings:
        $text1 = "suspicious string" ascii wide
        $hex1 = { 4D 5A 90 00 03 00 00 00 }
        $regex1 = /https?:\/\/[a-z0-9\-\.]+\.onion/

    condition:
        uint16(0) == 0x5A4D and
        filesize < 5MB and
        (2 of ($text*) or $hex1) and
        $regex1
}
```

## String Types

### Text strings
```yara
$s1 = "CreateRemoteThread"          // Exact ASCII match
$s2 = "kernel32.dll" ascii wide     // Match both ASCII and UTF-16
$s3 = "password" nocase             // Case-insensitive
$s4 = "cmd /c" ascii wide nocase    // Combined modifiers
$s5 = "C:\\Windows\\Temp\\"         // Escaped backslashes
```

### Hex strings
```yara
$h1 = { 4D 5A 90 00 }              // Exact bytes (PE header)
$h2 = { 4D 5A ?? ?? 03 00 }        // Wildcards (?? = any byte)
$h3 = { 4D 5A [2-4] 03 00 }        // Jump (2-4 bytes between)
$h4 = { 4D 5A ( 90 00 | 00 00 ) }  // Alternation (OR)
```

### Regular expressions
```yara
$r1 = /https?:\/\/[\w\-\.]+/       // URL pattern
$r2 = /[A-Za-z0-9+\/]{50,}={0,2}/  // Base64 encoded content
$r3 = /(\d{1,3}\.){3}\d{1,3}/      // IP address pattern
```

## Conditions

### File properties
```yara
uint16(0) == 0x5A4D                 // PE file (MZ header)
uint32(0) == 0x464C457F             // ELF file
uint32(0) == 0xBEBAFECA             // Mach-O fat binary
filesize < 10MB                     // File size limit
```

### String matching
```yara
all of them                         // All strings must match
any of them                         // At least one string
2 of ($text*)                       // At least 2 of $text group
3 of ($s1, $s2, $s3, $s4)           // 3 of these 4 strings
$s1 at 0                            // $s1 at offset 0
$s1 in (0..1024)                    // $s1 in first 1KB
#s1 > 3                             // $s1 occurs more than 3 times
```

### PE module (Windows executables)
```yara
import "pe"

pe.number_of_sections > 5
pe.imports("kernel32.dll", "VirtualAlloc")
pe.exports("DllRegisterServer")
pe.is_dll()
pe.timestamp > 1704067200           // Compiled after 2024-01-01
```

## Best Practices

### Do
- Start with a PE/ELF/file type check to limit scope
- Add a filesize constraint to avoid scanning large files
- Use unique strings - prefer byte sequences or specific API combinations over generic strings
- Combine multiple indicators (string + structure + behavior)
- Include metadata with reference to source intelligence
- Test against both malicious samples AND clean files (false positive testing)

### Don't
- Match on single, common strings (e.g., just "http://" or "cmd.exe")
- Create rules without file type constraints (performance killer)
- Use overly broad regex patterns
- Forget to escape special characters in hex patterns
- Write rules that match on packer/crypter artifacts only (will match unrelated packed software)

## Common Malware Patterns

### Encoded PowerShell
```yara
rule Encoded_PowerShell_Execution {
    meta:
        description = "Detects base64 encoded PowerShell commands"
        mitre_attack = "T1059.001"
    strings:
        $enc1 = "-EncodedCommand" ascii nocase
        $enc2 = "-enc " ascii nocase
        $enc3 = "FromBase64String" ascii nocase
        $b64 = /[A-Za-z0-9+\/]{100,}={0,2}/ ascii
    condition:
        any of ($enc*) and $b64
}
```

### Suspicious PE imports
```yara
import "pe"
rule Suspicious_Injection_Imports {
    meta:
        description = "PE with process injection API imports"
        mitre_attack = "T1055"
    condition:
        pe.imports("kernel32.dll", "VirtualAllocEx") and
        pe.imports("kernel32.dll", "WriteProcessMemory") and
        pe.imports("kernel32.dll", "CreateRemoteThread")
}
```

## Naming Convention

- `APT_GROUPNAME_MALWARENAME` - For attributed malware
- `MAL_MALWARENAME_VARIANT` - For named malware families
- `SUSP_BEHAVIOR_CONTEXT` - For suspicious behavior patterns
- `HUNT_TARGET_TECHNIQUE` - For threat hunting queries

Use the same name for the rule and, when the user wants a file (`generate_file`), for the `.yar` file name.

## Validation against a real corpus

Before deploying a rule beyond your test set, validate it against a broader sample collection.

**With the VirusTotal integration** (hunting and retrohunt need a VirusTotal licence that includes them; both consume quota):
- `create_vt_retrohunt_job` - scan the historical corpus with the rule. Poll with `get_vt_retrohunt_job`, then read hits with `get_vt_retrohunt_matches`. Review the matches: goodware or unrelated families mean the rule is too broad; zero matches on known family samples mean it is too narrow.
- `create_vt_hunting_ruleset` - deploy the rule as a live hunting ruleset so new submissions that match raise notifications (`list_vt_hunting_notifications`). This is a standing configuration: create it only when the user asks for ongoing hunting, and say so.
- `search_vt_intelligence` - retrieve a candidate sample set for the family before writing the rule, to build the test suite.
- `get_vt_related_objects` - find parents and children of a known sample (execution parents, dropped files, bundled files) to make sure the rule fires across the dropper-payload chain, not just on one layer.

**With the ANY.RUN integration:** `search_anyrun_yara` - run the rule over ANY.RUN's sandbox sample corpus to find additional matching submissions and their analyses.

**With the abuse.ch integration:** `search_abusech_malware` - pull recent MalwareBazaar samples of the family by tag or signature as known-bad test material.

Record the validation result in the rule metadata or the answer: corpus used, number of matches, false positives found, date.

## Related skills

- **Hash Investigation**, **Malware Analysis** - produce the structural and behavioural intelligence that informs the rule.
- **Threat Intel Sources** - how to read VirusTotal, MalwareBazaar and ANY.RUN results.
- **Sigma Rule Writing** - behavioural-detection counterpart for log telemetry.
