# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

<#
.SYNOPSIS
    Fails when a Windows binary imports the Visual C++ runtime DLLs.

.DESCRIPTION
    A runner that imports vcruntime140.dll does not start on a machine without
    the Visual C++ Redistributable ("VCRUNTIME140.dll was not found"), and a
    runner that updated itself to such a build cannot update itself out of it.
    build.rs links the runtime into the binary; this reads the PE
    import table (regular and delay-load) and refuses any Visual C++ runtime
    DLL: vcruntime*, msvcp*, msvcr<version>, vccorlib*, concrt*, and the debug
    ucrtbased.dll. The Universal CRT (ucrtbase.dll, api-ms-win-crt-*) ships with
    Windows 10 and later and is allowed.

    A binary whose import table cannot be read, or that imports neither
    KERNEL32.dll nor a core API set, is refused too: a check that read nothing
    would pass anything.

    -ExpectDynamic inverts the verdict. CI runs it on a build that does NOT
    link the runtime statically, to prove the check sees an import when there
    is one.
#>
param(
    [Parameter(Mandatory = $true)]
    [string] $Binary,

    [switch] $ExpectDynamic
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$runtimePattern = '^(vcruntime\d|msvcp\d|msvcr\d|vccorlib\d|concrt\d)|^ucrtbased\.dll$'

function Get-PeImports([string] $Path) {
    $bytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path).Path)

    function U16([long] $offset) { [long][BitConverter]::ToUInt16($bytes, [int]$offset) }
    function U32([long] $offset) { [long][BitConverter]::ToUInt32($bytes, [int]$offset) }
    function U64([long] $offset) { [long][BitConverter]::ToUInt64($bytes, [int]$offset) }

    if ($bytes.Length -lt 64 -or (U16 0) -ne 0x5A4D) { throw "$Path is not a PE file (no MZ header)" }
    $pe = U32 0x3C
    if ($pe + 24 -gt $bytes.Length -or (U32 $pe) -ne 0x00004550) { throw "$Path is not a PE file (no PE signature)" }
    $coff = $pe + 4
    $sectionCount = U16 ($coff + 2)
    $optionalSize = U16 ($coff + 16)
    $optional = $coff + 20
    switch (U16 $optional) {
        0x10B { $imageBase = U32 ($optional + 28); $directories = $optional + 96 }
        0x20B { $imageBase = U64 ($optional + 24); $directories = $optional + 112 }
        default { throw "$Path has an unknown optional header magic" }
    }
    $directoryCount = U32 ($directories - 4)

    $sections = @()
    $table = $optional + $optionalSize
    for ($i = 0; $i -lt $sectionCount; $i++) {
        $s = $table + 40 * $i
        $sections += [pscustomobject]@{
            Va   = U32 ($s + 12)
            Size = [Math]::Max((U32 ($s + 8)), (U32 ($s + 16)))
            Raw  = U32 ($s + 20)
        }
    }

    function ToOffset([long] $rva) {
        foreach ($s in $sections) {
            if ($rva -ge $s.Va -and $rva -lt $s.Va + $s.Size) { return $rva - $s.Va + $s.Raw }
        }
        throw "RVA 0x$('{0:X}' -f $rva) is outside every section"
    }

    function ReadName([long] $rva) {
        $start = ToOffset $rva
        $end = $start
        while ($end -lt $bytes.Length -and $bytes[$end] -ne 0) { $end++ }
        [Text.Encoding]::ASCII.GetString($bytes, [int]$start, [int]($end - $start))
    }

    # Both tables end with an all-zero descriptor; 4096 entries bound a
    # malformed one.
    $names = @()
    if ($directoryCount -gt 1 -and (U32 ($directories + 8)) -ne 0) {
        $descriptor = ToOffset (U32 ($directories + 8))
        $nameRva = U32 ($descriptor + 12)
        while ($nameRva -ne 0 -and $names.Count -lt 4096) {
            $names += ReadName $nameRva
            $descriptor += 20
            $nameRva = U32 ($descriptor + 12)
        }
    }
    if ($directoryCount -gt 13 -and (U32 ($directories + 13 * 8)) -ne 0) {
        $descriptor = ToOffset (U32 ($directories + 13 * 8))
        $nameRef = U32 ($descriptor + 4)
        while ($nameRef -ne 0 -and $names.Count -lt 8192) {
            # Attribute bit 0 clear: the pre-VC7 form, which stores addresses.
            if (((U32 $descriptor) -band 1) -eq 0) { $nameRef -= $imageBase }
            $names += ReadName $nameRef
            $descriptor += 32
            $nameRef = U32 ($descriptor + 4)
        }
    }
    $names | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object -Unique
}

$imports = @(Get-PeImports $Binary)
Write-Host "$Binary imports:"
$imports | ForEach-Object { Write-Host "  $_" }

if (-not ($imports | Where-Object { $_ -eq 'kernel32.dll' -or $_ -like 'api-ms-win-core-*' })) {
    Write-Host "::error::neither KERNEL32.dll nor a core API set among the imports of $Binary - the import table was not read"
    exit 1
}

$runtime = @($imports | Where-Object { $_ -match $runtimePattern })
if ($ExpectDynamic) {
    if ($runtime.Count -eq 0) {
        Write-Host "::error::$Binary was expected to import the Visual C++ runtime and imports none of it - the check cannot see an import"
        exit 1
    }
    Write-Host "Imports the Visual C++ runtime, as expected: $($runtime -join ', ')"
    exit 0
}
if ($runtime.Count -gt 0) {
    Write-Host "::error::$Binary imports the Visual C++ runtime ($($runtime -join ', ')): it will not start on a machine without the Visual C++ Redistributable. build.rs links it statically; check that tauri-build still honours WindowsAttributes::static_vc_runtime."
    exit 1
}
Write-Host "No Visual C++ runtime import."
