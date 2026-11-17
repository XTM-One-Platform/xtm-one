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
    Runs the XTM One Desktop NSIS installer over an "XTM One Runner" install
    (the former product name) and checks that installer-hooks.nsh retires
    it without touching the state the two share.

.DESCRIPTION
    For CI only (runner-ci.yml), on a throwaway Windows machine: it writes the
    current user's registry, Start menu, desktop and AppData, and runs the
    installer and its uninstaller.

    1. The updater's path (/S /UPDATE): the Installed apps entry and the
       remembered location of the former install are gone, its program files
       are gone, its shortcuts and taskbar pin start the new build, and so does
       its autostart entry. The config and the webview profile are untouched.
    2. A person running the installer (/S): same retirement, with autostart off
       it stays off, and a shortcut of that name that starts something else is
       left alone.
    3. The installer's uninstall (uninstall.exe in place with _?=, what an
       interactive upgrade runs): the shortcuts step 1 and 2 pointed at this
       build go with it, the unrelated one stays, and the config and the
       webview profile are kept.
    4. A real uninstall (the registered uninstaller, which runs a copy of
       itself from TEMP): the repointed shortcuts go, and so do the config,
       the webview profile and the autostart entry.
    5. An "XTM One Runner" registration naming the folder this build is
       installed in, spelled the same way or differently: this build is kept
       and only the registration goes.
    6. An "XTM One Runner" entry published by someone else: left alone.
#>
param(
    [Parameter(Mandatory = $true)]
    [string] $Installer
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$legacyName = 'XTM One Runner'
$newName = 'XTM One Desktop'
$exe = 'xtm-one-runner.exe'
$legacyDir = Join-Path $env:LOCALAPPDATA $legacyName
$newDir = Join-Path $env:LOCALAPPDATA $newName
$legacyExe = Join-Path $legacyDir $exe
$newExe = Join-Path $newDir $exe
$uninstallRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
$legacyUninstall = Join-Path $uninstallRoot $legacyName
$newUninstall = Join-Path $uninstallRoot $newName
$legacyLocation = "HKCU:\Software\Filigran\$legacyName"
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$autostartEntry = 'XTM One Runner'
$startMenu = [Environment]::GetFolderPath('Programs')
$desktop = [Environment]::GetFolderPath('Desktop')
$taskbarPins = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
$legacyShortcuts = @(
    (Join-Path $startMenu "$legacyName.lnk"),
    (Join-Path $desktop "$legacyName.lnk"),
    (Join-Path $taskbarPins "$legacyName.lnk")
)
$configFile = Join-Path $env:APPDATA 'Filigran\xtm-one-runner\config\config.json'
$webviewFile = Join-Path $env:LOCALAPPDATA 'io.filigran.xtm-one-runner\EBWebView\Local State'
$shell = New-Object -ComObject WScript.Shell
$failures = New-Object System.Collections.Generic.List[string]

function Assert-That([bool] $ok, [string] $what) {
    if ($ok) {
        Write-Host "ok   $what"
    } else {
        Write-Host "FAIL $what"
        $failures.Add($what)
    }
}

function New-Shortcut([string] $path, [string] $target) {
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $target
    $link.WorkingDirectory = Split-Path $target
    $link.Save()
}

function Get-ShortcutTarget([string] $path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    return $shell.CreateShortcut($path).TargetPath
}

function Get-ShortcutWorkingDirectory([string] $path) {
    return $shell.CreateShortcut($path).WorkingDirectory
}

# New-Item -Force on an existing registry key clears its values.
function Initialize-Key([string] $path) {
    if (-not (Test-Path -Path $path)) { New-Item -Path $path -Force | Out-Null }
}

function Get-RunEntry {
    $key = Get-ItemProperty -Path $runKey -ErrorAction SilentlyContinue
    if ($null -eq $key) { return $null }
    $prop = $key.PSObject.Properties[$autostartEntry]
    if ($null -eq $prop) { return $null }
    return [string] $prop.Value
}

# The registration, files and shortcuts a baseline build left on the machine.
function Install-LegacyRunner([bool] $autostart) {
    New-Item -ItemType Directory -Force -Path $legacyDir | Out-Null
    Set-Content -LiteralPath $legacyExe -Value 'former build'
    Set-Content -LiteralPath (Join-Path $legacyDir 'uninstall.exe') -Value 'former uninstaller'
    Initialize-Key $legacyUninstall
    Set-ItemProperty -Path $legacyUninstall -Name DisplayName -Value $legacyName
    Set-ItemProperty -Path $legacyUninstall -Name DisplayVersion -Value '1.250924.100'
    Set-ItemProperty -Path $legacyUninstall -Name Publisher -Value 'Filigran'
    Set-ItemProperty -Path $legacyUninstall -Name InstallLocation -Value "`"$legacyDir`""
    Set-ItemProperty -Path $legacyUninstall -Name UninstallString -Value "`"$legacyDir\uninstall.exe`""
    Initialize-Key $legacyLocation
    Set-ItemProperty -Path $legacyLocation -Name '(default)' -Value $legacyDir
    foreach ($shortcut in $legacyShortcuts) { New-Shortcut $shortcut $legacyExe }
    if ($autostart) {
        Initialize-Key $runKey
        Set-ItemProperty -Path $runKey -Name $autostartEntry -Value "`"$legacyExe`" --autostarted"
    } else {
        Remove-ItemProperty -Path $runKey -Name $autostartEntry -ErrorAction SilentlyContinue
    }
}

# Start-Process joins and quotes nothing, which is what NSIS wants: `_?=` must
# come last and unquoted, spaces included.
function Invoke-Program([string] $path, [string] $arguments) {
    $process = Start-Process -FilePath $path -ArgumentList $arguments -Wait -PassThru
    Assert-That ($process.ExitCode -eq 0) "$(Split-Path -Leaf $path) $arguments exits 0 (got $($process.ExitCode))"
}

function Assert-LegacyRetired([string] $scenario) {
    Assert-That (Test-Path -LiteralPath $newExe) "${scenario}: the new build is installed in $newDir"
    Assert-That (Test-Path -Path $newUninstall) "${scenario}: '$newName' is registered in Installed apps"
    Assert-That (-not (Test-Path -Path $legacyUninstall)) "${scenario}: '$legacyName' is no longer in Installed apps"
    Assert-That (-not (Test-Path -Path $legacyLocation)) "${scenario}: the former remembered location is gone"
    Assert-That (-not (Test-Path -LiteralPath $legacyExe)) "${scenario}: the former build is gone"
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $legacyDir 'uninstall.exe'))) "${scenario}: the former uninstaller is gone"
    Assert-That (-not (Test-Path -LiteralPath $legacyDir)) "${scenario}: the former folder is gone"
    Assert-That (Test-Path -LiteralPath $configFile) "${scenario}: the config is kept"
    Assert-That (Test-Path -LiteralPath $webviewFile) "${scenario}: the webview profile is kept"
}

# The state both installs share: removing it is what the former uninstaller
# would have done.
New-Item -ItemType Directory -Force -Path (Split-Path $configFile), (Split-Path $webviewFile) | Out-Null
Set-Content -LiteralPath $configFile -Value '{"server_url":"https://one.example.test","runner_id":"00000000-0000-0000-0000-000000000000"}'
Set-Content -LiteralPath $webviewFile -Value '{}'

Write-Host "== 1. The updater's path: /S /UPDATE over an '$legacyName' install"
Install-LegacyRunner -autostart $true
Invoke-Program $Installer '/S /UPDATE'
Assert-LegacyRetired 'update'
# The former install is seeded with what an install of this app holds - the
# binary and its uninstaller: the WebView2 loader is linked in and the app ships
# no resources. An install that held more would leave its folder behind.
$installed = @(Get-ChildItem -LiteralPath $newDir -Force | ForEach-Object { $_.Name } | Sort-Object)
Assert-That (($installed -join ',') -eq 'uninstall.exe,xtm-one-runner.exe') "update: an install holds exactly the files the former one is seeded with (got '$($installed -join ',')')"
foreach ($shortcut in $legacyShortcuts) {
    $target = Get-ShortcutTarget $shortcut
    Assert-That ($target -eq $newExe) "update: $shortcut starts the new build (got '$target')"
    if ($target) {
        $dir = Get-ShortcutWorkingDirectory $shortcut
        Assert-That ($dir -eq $newDir) "update: $shortcut starts in $newDir (got '$dir')"
    }
}
$run = Get-RunEntry
Assert-That ($run -eq "`"$newExe`" --autostarted") "update: the autostart entry starts the new build (got '$run')"

Write-Host "== 2. A person running the installer: /S over an '$legacyName' install, autostart off"
Install-LegacyRunner -autostart $false
$unrelated = Join-Path $desktop "$legacyName.lnk"
New-Shortcut $unrelated (Join-Path $env:WINDIR 'notepad.exe')
Invoke-Program $Installer '/S'
Assert-LegacyRetired 'install'
foreach ($shortcut in @((Join-Path $startMenu "$legacyName.lnk"), (Join-Path $taskbarPins "$legacyName.lnk"))) {
    $target = Get-ShortcutTarget $shortcut
    Assert-That ($target -eq $newExe) "install: $shortcut starts the new build (got '$target')"
}
$target = Get-ShortcutTarget $unrelated
Assert-That ($target -like '*\notepad.exe') "install: a shortcut of that name that starts something else is left alone (got '$target')"
Assert-That ($null -eq (Get-RunEntry)) 'install: autostart off stays off'

Write-Host "== 3. The installer's uninstall of '$newName': in place, with _?="
Invoke-Program (Join-Path $newDir 'uninstall.exe') "/S _?=$newDir"
foreach ($shortcut in @((Join-Path $startMenu "$legacyName.lnk"), (Join-Path $taskbarPins "$legacyName.lnk"))) {
    Assert-That (-not (Test-Path -LiteralPath $shortcut)) "installer uninstall: $shortcut is removed with the build it starts"
}
$target = Get-ShortcutTarget $unrelated
Assert-That ($target -like '*\notepad.exe') "installer uninstall: the unrelated shortcut is left alone (got '$target')"
Assert-That (Test-Path -LiteralPath $configFile) 'installer uninstall: the config is kept (an upgrade step never unenrolls)'
Assert-That (Test-Path -LiteralPath $webviewFile) 'installer uninstall: the webview profile is kept'

Write-Host "== 4. A real uninstall of '$newName': the registered uninstaller"
Install-LegacyRunner -autostart $true
Invoke-Program $Installer '/S /UPDATE'
Assert-That ($null -ne (Get-ShortcutTarget (Join-Path $startMenu "$legacyName.lnk"))) 'real uninstall: set up with a repointed shortcut'
$uninstallString = (Get-ItemProperty -Path $newUninstall).UninstallString.Trim('"')
# Without _?= the uninstaller copies itself to TEMP, starts that copy and
# exits; -Wait also waits for the copy, and the loop covers a copy it missed.
Invoke-Program $uninstallString '/S'
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    $copies = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path -like '*\~nsu*.tmp\*' } catch { $false }
    })
    if ($copies.Count -eq 0 -and -not (Test-Path -Path $newUninstall)) { break }
    Start-Sleep -Milliseconds 500
}
Assert-That (-not (Test-Path -Path $newUninstall)) "real uninstall: '$newName' is no longer in Installed apps"
foreach ($shortcut in $legacyShortcuts) {
    Assert-That (-not (Test-Path -LiteralPath $shortcut)) "real uninstall: $shortcut is removed with the build it starts"
}
Assert-That (-not (Test-Path -LiteralPath $configFile)) 'real uninstall: the config is removed'
Assert-That (-not (Test-Path -LiteralPath $webviewFile)) 'real uninstall: the webview profile is removed'
Assert-That ($null -eq (Get-RunEntry)) 'real uninstall: the autostart entry is removed'

Write-Host "== 5. An '$legacyName' registration naming this build's folder"
foreach ($spelling in @($newDir, ($newDir.ToUpperInvariant() + '\'))) {
    Initialize-Key $legacyUninstall
    Set-ItemProperty -Path $legacyUninstall -Name DisplayName -Value $legacyName
    Set-ItemProperty -Path $legacyUninstall -Name Publisher -Value 'Filigran'
    Set-ItemProperty -Path $legacyUninstall -Name InstallLocation -Value "`"$spelling`""
    Set-ItemProperty -Path $legacyUninstall -Name UninstallString -Value "`"$spelling\uninstall.exe`""
    Initialize-Key $legacyLocation
    Set-ItemProperty -Path $legacyLocation -Name '(default)' -Value $spelling
    Invoke-Program $Installer '/S /UPDATE'
    Assert-That (Test-Path -LiteralPath $newExe) "same folder ($spelling): this build is kept"
    Assert-That (Test-Path -LiteralPath (Join-Path $newDir 'uninstall.exe')) "same folder ($spelling): this build's uninstaller is kept"
    Assert-That (Test-Path -Path $newUninstall) "same folder ($spelling): '$newName' stays in Installed apps"
    Assert-That (-not (Test-Path -Path $legacyUninstall)) "same folder ($spelling): '$legacyName' is no longer in Installed apps"
    Assert-That (-not (Test-Path -Path $legacyLocation)) "same folder ($spelling): the former remembered location is gone"
}

Write-Host "== 6. An '$legacyName' entry published by someone else"
$foreignDir = Join-Path $env:LOCALAPPDATA 'Someone Else'
New-Item -ItemType Directory -Force -Path $foreignDir | Out-Null
Set-Content -LiteralPath (Join-Path $foreignDir $exe) -Value 'not ours'
Set-Content -LiteralPath (Join-Path $foreignDir 'uninstall.exe') -Value 'not ours'
Initialize-Key $legacyUninstall
Set-ItemProperty -Path $legacyUninstall -Name DisplayName -Value $legacyName
Set-ItemProperty -Path $legacyUninstall -Name Publisher -Value 'Someone Else'
Set-ItemProperty -Path $legacyUninstall -Name InstallLocation -Value "`"$foreignDir`""
Set-ItemProperty -Path $legacyUninstall -Name UninstallString -Value "`"$foreignDir\uninstall.exe`""
Invoke-Program $Installer '/S /UPDATE'
Assert-That (Test-Path -Path $legacyUninstall) 'foreign: its Installed apps entry is left alone'
Assert-That (Test-Path -LiteralPath (Join-Path $foreignDir $exe)) 'foreign: its program is left alone'
Assert-That (Test-Path -LiteralPath (Join-Path $foreignDir 'uninstall.exe')) 'foreign: its uninstaller is left alone'

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "$($failures.Count) check(s) failed:"
    foreach ($failure in $failures) { Write-Host "  - $failure" }
    exit 1
}
Write-Host ""
Write-Host "All checks passed."
