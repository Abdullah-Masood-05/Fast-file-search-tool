; Inno Setup script for Fast File Search Pro (Inno Setup 6.3+ / 7).
;
; Build after `python build.py` (standalone):
;   iscc /DAppVersion=1.2.0 packaging\windows\setup.iss
; Optional defines: SourceDir (Nuitka standalone folder), OutputDir, OutputBaseFilename.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\FastFileSearchPro"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "Fast-File-Search-Pro-v" + AppVersion + "-Windows-x64-Setup"
#endif
#define AppName "Fast File Search Pro"
#define AppExe "FastFileSearchPro.exe"
#define AppPublisher "Abdullah Masood"
#define AppURL "https://github.com/Abdullah-Masood-05/Fast-file-search-tool"

[Setup]
; Keep AppId stable across releases so upgrades replace the previous install.
AppId={{6F0B8E52-3C1D-4E7A-9B4F-5A2D7C9E1F38}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
SetupIconFile=..\..\files.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
