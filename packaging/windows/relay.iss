; Inno Setup script for SDR Tools Relay. Built by .github/workflows/windows.yml:
;   iscc /DVersion=0.1.0 /DSource=dist\SDR Tools Relay packaging\windows\relay.iss
; Installs for the current user (no administrator needed), so updates run silently.

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "..\..\dist\SDR Tools Relay"
#endif

[Setup]
AppId={{6F2B1E7C-3D5A-4C8B-9E41-5D2A7C0B9F31}
AppName=SDR Tools Relay
AppVersion={#Version}
AppPublisher=ssjrocks
AppPublisherURL=https://github.com/ssjrocks/sdr-tools-relay
DefaultDirName={localappdata}\Programs\SDR Tools Relay
DefaultGroupName=SDR Tools Relay
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=SDR-Tools-Relay-Setup-{#Version}
OutputDir=..\..\dist
SetupIconFile=..\..\build\icon.ico
UninstallDisplayIcon={app}\SDR Tools Relay.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
LicenseFile=..\..\LICENSE

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userprograms}\SDR Tools Relay"; Filename: "{app}\SDR Tools Relay.exe"
Name: "{userdesktop}\SDR Tools Relay"; Filename: "{app}\SDR Tools Relay.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Registry]
; The relay's own "Start when I log in" writes this value; remove it with the app.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "SDRToolsRelay"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\SDR Tools Relay.exe"; Description: "Start SDR Tools Relay"; Flags: postinstall nowait skipifsilent
; After a silent update (from the app), start the new version.
Filename: "{app}\SDR Tools Relay.exe"; Flags: nowait; Check: LaunchAfterSilentInstall

[Code]
// /NOLAUNCH=1 skips that (the CI's test install, whose runner would wait for the app to exit).
function LaunchAfterSilentInstall: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:NOLAUNCH|0}') = '0');
end;
