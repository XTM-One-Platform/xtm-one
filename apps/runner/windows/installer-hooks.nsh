; XTM One Desktop - NSIS installer hooks (wired via bundle.windows.nsis.installerHooks).
;
; Uninstall means uninstall. The stock Tauri uninstaller removes the
; app folder and - only behind the "Delete the application data" checkbox -
; the webview profile under the bundle identifier. It knows nothing about the
; runner's real local state:
;
;   * config.json under $APPDATA\Filigran\xtm-one-runner\  (directories crate,
;     ProjectDirs "one" / "Filigran" / "xtm-one-runner"), and
;   * the enrollment credential in the Windows Credential Manager (keyring),
;     with the chat window's web session kept next to it.
;
; Leaving those behind made the next install boot straight into an "enrolled"
; state with a revoked credential instead of the Connect form. These hooks
; wipe them on every REAL uninstall, whatever the checkbox says.
;
; CRITICAL guard: upgrades run this same uninstaller. When a newer installer
; replaces an existing install, Tauri's template executes the OLD uninstaller
; in place - `uninstall.exe _?=<dir>`, with /UPDATE appended when the
; auto-updater drives it. A user-initiated uninstall (Installed apps, winget,
; uninstall.exe by hand) starts uninstall.exe, which copies itself to $TEMP and
; runs that copy. NSIS cuts ` _?=<dir>` off $CMDLINE before any script code
; runs, so that marker cannot be read here; where the uninstaller runs from
; can. Wiping on the upgrade path would silently unenroll every runner on
; every update, so the wipe runs ONLY without /UPDATE and outside the install
; folder.
;
; The PLATFORM-side runner record is deliberately never touched (an uninstall
; may run offline); revoke it on the XTM One Desktop page.
;
; The install of the former product name. Tauri keys an NSIS install by its
; PRODUCT name - the Installed apps entry, the remembered location, the folder,
; the shortcuts - so the update that turned "XTM One Runner" into "XTM One
; Desktop" did not replace the old install: it installed into a new
; folder and, in update mode, created no shortcut, trusting the ones of its own
; name. Left behind were the old build, which its shortcuts, pins and autostart
; entry still start, and its uninstaller: run from Installed apps, that is a
; REAL uninstall under these hooks, and it deletes the enrollment, the config,
; the webview profile and the autostart entry this install shares with it.
; POSTINSTALL retires that install instead: its shortcuts and autostart entry
; start this build, its program files and its registration go. Its state is
; left alone - it is this install's now. It is ours when Filigran's remembered
; location names it or Filigran published its Installed apps entry; anything
; else under that name is left alone.

Var WipeLocalState

!define LEGACY_PRODUCTNAME "XTM One Runner"
!define LEGACY_UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\XTM One Runner"
!define LEGACY_MANUPRODUCTKEY "Software\Filigran\XTM One Runner"
; The app registers its autostart entry under this name whatever the product is
; called (AUTOSTART_ENTRY_NAME in src/gui.rs).
!define AUTOSTART_ENTRY "XTM One Runner"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define TASKBAR_PINS "$APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"

; One spelling per folder, to tell whether two paths are the same folder: the
; short form when it exists (it folds case and long names), no trailing
; backslash. `scratch` is clobbered.
!macro CanonicalFolder path out scratch
  GetFullPathName /SHORT ${out} "${path}"
  ${If} ${out} == ""
    GetFullPathName ${out} "${path}"
  ${EndIf}
  ${If} ${out} == ""
    StrCpy ${out} "${path}"
  ${EndIf}
  StrCpy ${scratch} ${out} 1 -1
  ${If} ${scratch} == "\"
    StrCpy ${out} ${out} -1
  ${EndIf}
!macroend

; A shortcut under the former name that starts `from` is rewritten in place to
; start this build: same file, so a Start or taskbar pin made from it keeps
; working. CreateShortcut takes the working directory from the output path.
!macro RepointLegacyShortcut dir from
  !insertmacro IsShortcutTarget "${dir}\${LEGACY_PRODUCTNAME}.lnk" "${from}"
  Pop $0
  ${If} $0 = 1
    CreateShortcut "${dir}\${LEGACY_PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    !insertmacro SetLnkAppUserModelId "${dir}\${LEGACY_PRODUCTNAME}.lnk"
  ${EndIf}
!macroend

; The same shortcuts on a real uninstall of this install, which the stock
; uninstaller only looks for under the current product name.
!macro RemoveLegacyShortcut dir
  !insertmacro IsShortcutTarget "${dir}\${LEGACY_PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
  Pop $0
  ${If} $0 = 1
    !insertmacro UnpinShortcut "${dir}\${LEGACY_PRODUCTNAME}.lnk"
    Delete "${dir}\${LEGACY_PRODUCTNAME}.lnk"
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $R6
  Push $R7
  Push $R8
  Push $R9
  ; Every build of the former product was published by Filigran.
  StrCpy $R8 0
  ReadRegStr $0 HKCU "${LEGACY_UNINSTKEY}" "Publisher"
  ${If} $0 == "${MANUFACTURER}"
    StrCpy $R8 1
  ${EndIf}
  ReadRegStr $R9 HKCU "${LEGACY_MANUPRODUCTKEY}" ""
  ${If} $R9 == ""
  ${AndIf} $R8 = 1
    ; Written quoted.
    ReadRegStr $R9 HKCU "${LEGACY_UNINSTKEY}" "InstallLocation"
    StrCpy $0 $R9 1
    ${If} $0 == '"'
      StrCpy $R9 $R9 -1 1
    ${EndIf}
  ${EndIf}
  ; The former install may have been registered at this very folder (a person
  ; who installed this build over it): its files are this install's then, and
  ; its uninstall.exe is this install's uninstaller, so only the registration
  ; goes.
  StrCpy $R7 ""
  StrCpy $R6 ""
  ${If} $R9 != ""
    !insertmacro CanonicalFolder $R9 $R7 $0
    !insertmacro CanonicalFolder $INSTDIR $R6 $0
  ${EndIf}
  ${If} $R9 != ""
  ${AndIf} $R7 != $R6
    SetOutPath $INSTDIR
    !insertmacro RepointLegacyShortcut "$SMPROGRAMS" "$R9\${MAINBINARYNAME}.exe"
    !insertmacro RepointLegacyShortcut "$DESKTOP" "$R9\${MAINBINARYNAME}.exe"
    !insertmacro RepointLegacyShortcut "${TASKBAR_PINS}" "$R9\${MAINBINARYNAME}.exe"
    ; The app rewrites this entry at every start; this covers a sign-in that
    ; comes before its first one. Absent stays absent (autostart off).
    ReadRegStr $0 HKCU "${RUN_KEY}" "${AUTOSTART_ENTRY}"
    ${If} $0 != ""
      ${WordReplace} $0 "$R9\${MAINBINARYNAME}.exe" "$INSTDIR\${MAINBINARYNAME}.exe" "+" $1
      ${If} $1 != $0
        WriteRegStr HKCU "${RUN_KEY}" "${AUTOSTART_ENTRY}" $1
      ${EndIf}
    ${EndIf}
    ; Named files and a bare RMDir, never a recursive delete: the folder comes
    ; from the registry. No /REBOOTOK either - it would offer a reboot on the
    ; finish page; a binary still running stays behind, unregistered.
    Delete "$R9\${MAINBINARYNAME}.exe"
    Delete "$R9\uninstall.exe"
    RMDir "$R9"
  ${EndIf}
  ; The registration goes even without a known folder: an Installed apps entry
  ; is the uninstaller one click away.
  ${If} $R8 = 1
    DeleteRegKey HKCU "${LEGACY_UNINSTKEY}"
  ${EndIf}
  DeleteRegKey HKCU "${LEGACY_MANUPRODUCTKEY}"
  Pop $R9
  Pop $R8
  Pop $R7
  Pop $R6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; Decide once, before the app is killed and the files are removed, whether
  ; this run is a real uninstall (wipe) or an upgrade step (preserve).
  StrCpy $WipeLocalState 0
  ${If} $UpdateMode <> 1
    Push $0
    Push $1
    Push $2
    !insertmacro CanonicalFolder $EXEDIR $0 $2
    !insertmacro CanonicalFolder $INSTDIR $1 $2
    ${If} $0 != $1
      StrCpy $WipeLocalState 1
    ${EndIf}
    Pop $2
    Pop $1
    Pop $0
  ${EndIf}

  ; The credential can only be addressed through the app's own keyring entry
  ; (service name + the runner id stored in config.json), so let the binary
  ; delete it while it still exists. `reset` removes the credential, the web
  ; session kept with it and the config file, never touches the network, and
  ; exits immediately; it is a
  ; windowless (GUI-subsystem) binary, so nothing flashes. Failures are
  ; ignored on purpose - the post-uninstall sweep below still runs.
  ${If} $WipeLocalState = 1
  ${AndIf} ${FileExists} "$INSTDIR\xtm-one-runner.exe"
    ExecWait '"$INSTDIR\xtm-one-runner.exe" reset'
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; Belt and braces once the files are gone: the whole per-app state tree
  ; (config/data under Roaming, cache under Local), the webview profiles, the
  ; autostart entry, and the installer's own remembered-location registry
  ; crumbs that the stock checkbox used to cover. The bare RMDir on the
  ; vendor folders removes them only when empty - other Filigran software may
  ; live there.
  ${If} $WipeLocalState = 1
    RMDir /r "$APPDATA\Filigran\xtm-one-runner"
    RMDir "$APPDATA\Filigran"
    RMDir /r "$LOCALAPPDATA\Filigran\xtm-one-runner"
    RMDir "$LOCALAPPDATA\Filigran"
    RMDir /r "$APPDATA\${BUNDLEID}"
    RMDir /r "$LOCALAPPDATA\${BUNDLEID}"
    ; The autostart entry is registered under the technical name, not the
    ; product name (AUTOSTART_ENTRY_NAME in src/gui.rs); both are removed so
    ; no build's entry survives a wipe.
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "XTM One Runner"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${PRODUCTNAME}"
    DeleteRegKey SHCTX "${MANUPRODUCTKEY}"
    DeleteRegKey /ifempty SHCTX "${MANUKEY}"
    DeleteRegValue HKCU "${MANUPRODUCTKEY}" "Installer Language"
    DeleteRegKey /ifempty HKCU "${MANUPRODUCTKEY}"
    DeleteRegKey /ifempty HKCU "${MANUKEY}"
  ${EndIf}

  ${If} $UpdateMode <> 1
    Push $0
    Push $1
    Push $2
    Push $3
    !insertmacro RemoveLegacyShortcut "$SMPROGRAMS"
    !insertmacro RemoveLegacyShortcut "$DESKTOP"
    !insertmacro RemoveLegacyShortcut "${TASKBAR_PINS}"
    Pop $3
    Pop $2
    Pop $1
    Pop $0
  ${EndIf}
!macroend
