; Inno Setup script - real Windows installer for Clinic Sync (per-user, no admin needed)
#define AppVer GetEnv("CLINIC_VER")
[Setup]
AppId={{6B0B6C9E-5C1A-4D3B-9A52-5A1C11C0A001}
AppName=Salman Herbal Clinic
AppVersion={#AppVer}
AppPublisher=Salman Herbal Clinic
DefaultDirName={localappdata}\Programs\Salman Herbal Clinic
DefaultGroupName=Salman Herbal Clinic
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ClinicSync
SetupIconFile=..\pc\icon.ico
UninstallDisplayIcon={app}\ClinicSync.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: checkedonce

[Files]
Source: "..\build_app\ClinicSync.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Salman Herbal Clinic"; Filename: "{app}\ClinicSync.exe"
Name: "{autodesktop}\Salman Herbal Clinic"; Filename: "{app}\ClinicSync.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ClinicSync.exe"; Description: "Start Salman Herbal Clinic now"; Flags: nowait postinstall skipifsilent
