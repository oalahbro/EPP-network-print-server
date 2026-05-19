[Setup]
AppName=EPP Print Server
AppVersion=2.2
AppPublisher=EPP
AppPublisherURL=https://github.com/oalahbro
DefaultDirName=C:\EPP
DefaultGroupName=EPP Print Server
OutputDir=installer_output
OutputBaseFilename=EPP_Setup_v2.2
SetupIconFile=EPP.ico
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
UninstallDisplayIcon={app}\epp.exe

[Files]
Source: "dist\epp\epp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\epp\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "conf.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "EPP.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\EPP Print Server"; Filename: "{app}\epp.exe"; Parameters: "--tray"; IconFilename: "{app}\EPP.ico"
Name: "{group}\Uninstall EPP"; Filename: "{uninstallexe}"
Name: "{commondesktop}\EPP Print Server"; Filename: "{app}\epp.exe"; Parameters: "--launch"; IconFilename: "{app}\EPP.ico"
Name: "{userstartup}\EPP Print Server"; Filename: "{app}\epp.exe"; Parameters: "--tray"; IconFilename: "{app}\EPP.ico"

[Run]
; Register & start service
Filename: "sc.exe"; Parameters: "create EPPrintServer binPath= ""{app}\epp.exe"" start= auto DisplayName= ""EPP Print Server"""; StatusMsg: "Installing EPP Service..."; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "description EPPrintServer ""ESC/POS Print Server for thermal printers"""; StatusMsg: "Setting service description..."; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "start EPPrintServer"; StatusMsg: "Starting EPP Service..."; Flags: runhidden waituntilterminated
; Launch tray icon
Filename: "{app}\epp.exe"; Parameters: "--tray"; StatusMsg: "Starting EPP Tray..."; Flags: postinstall nowait

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/F /IM epp.exe"; Flags: runhidden waituntilterminated; RunOnceId: "KillTray"
Filename: "sc.exe"; Parameters: "stop EPPrintServer"; Flags: runhidden waituntilterminated; RunOnceId: "StopService"
Filename: "sc.exe"; Parameters: "delete EPPrintServer"; Flags: runhidden waituntilterminated; RunOnceId: "DeleteService"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    Exec('taskkill.exe', '/F /IM epp.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('sc.exe', 'stop EPPrintServer', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('sc.exe', 'delete EPPrintServer', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
