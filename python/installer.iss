; WiFi-Looking Windows installer
; NOTE: this file must be UTF-8 with BOM (Inno Setup 6 interprets
; non-ASCII text as ANSI otherwise, garbling the Chinese task text).
#define MyAppName "WiFi-Looking"
#define MyAppVersion "0.3.0"
#define MyAppPublisher "WiFi-Looking"
#define MyAppExeName "WiFi-Looking.exe"

[Setup]
AppId={{8E9F6E13-0F97-4E8B-8DE4-2AC2BC2F1A3B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={userpf}\WiFi-Looking
UsePreviousAppDir=yes
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=dist
OutputBaseFilename=WiFi-Looking-Setup
SetupIconFile=assets\app.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
AllowNoIcons=yes
ArchitecturesAllowed=x64compatible
MinVersion=10.0
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} installer

[Languages]
; Bundled translation file: the CI runner's Inno Setup install does not
; ship ChineseSimplified.isl, so compiler:Languages\... fails there.
; This is Inno Setup's own official Simplified Chinese messages file.
Name: "chinesesimplified"; MessagesFile: "installer-languages\ChineseSimplified.isl"
DefaultLanguage=chinesesimplified

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "autostart"; Description: "开机后自动启动 WiFi-Looking"; GroupDescription: "附加任务:"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; The seed config comes from dist\config.json, which build.py
; generates from DEFAULT_CONFIG (review bug 15): it must never be
; the developer's live runtime config, or developer rules would ship
; as "factory defaults". onlyifdoesntexist keeps an upgrading user's
; own config (and its rules) intact.
Source: "dist\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
; Autostart launches straight to the tray (--minimized, review bug 26):
; popping the full window on every logon trains users to disable it.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--minimized"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"
; config.json is deliberately NOT deleted: uninstall keeps the user's
; rules, and --reset-config (or deleting the file) restores defaults
; (review bug 27). The startup-folder shortcut above is removed by
; Inno Setup automatically with the [Icons] entries.
