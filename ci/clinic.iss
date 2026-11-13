; Inno Setup script - real Windows installer for Clinic Sync (installs to Program Files, adds firewall rule once)
#define AppVer GetEnv("CLINIC_VER")
[Setup]
AppId={{6B0B6C9E-5C1A-4D3B-9A52-5A1C11C0A001}
AppName=HealthClinic
AppVersion={#AppVer}
AppPublisher=HealthClinic
DefaultDirName={autopf}\HealthClinic
DefaultGroupName=HealthClinic
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=..\dist
OutputBaseFilename=HealthClinic
SetupIconFile=..\pc\icon.ico
UninstallDisplayIcon={app}\HealthClinic.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: checkedonce

[Files]
Source: "..\build_app\HealthClinic.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\HealthClinic"; Filename: "{app}\HealthClinic.exe"
Name: "{autodesktop}\HealthClinic"; Filename: "{app}\HealthClinic.exe"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Clinic Sync"" dir=in action=allow protocol=TCP localport=8765 profile=any"; Flags: runhidden
Filename: "{app}\HealthClinic.exe"; Description: "Start HealthClinic now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Clinic Sync"""; Flags: runhidden
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM HealthClinic.exe"; Flags: runhidden

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var R: Integer;
begin
  if CurStep = ssInstall then Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM HealthClinic.exe', '', SW_HIDE, ewWaitUntilTerminated, R);
end;
