; Better-money Windows 安装器（Inno Setup 6）
; 构建：build\build_installer.ps1（读取 app/version.py 并以 /DAppVersion 传入）
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define MyAppName "Better-money"
#define MyAppExeName "BetterMoney.exe"

[Setup]
AppId={{A7C3E2F5-9B4D-4E7A-8C61-2D5B0E9F3A42}
AppName={#MyAppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\Better Money
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
OutputDir=..\release
OutputBaseFilename=BetterMoney-Setup-{#AppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: checkedonce

[Files]
Source: "..\dist\BetterMoney\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{autoprograms}\Better-money"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Better-money"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 Better-money"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 默认不删除任何个人数据；个人数据只在用户勾选复选框并二次确认后删除

[Code]
var
  DeleteDataCheck: TCheckBox;

procedure InitializeWizard;
begin
  DeleteDataCheck := TCheckBox.Create(WizardForm.SelectTasksPage);
  DeleteDataCheck.Parent := WizardForm.SelectTasksPage;
  DeleteDataCheck.Top := WizardForm.TasksList.Top + WizardForm.TasksList.Height + 12;
  DeleteDataCheck.Width := WizardForm.SelectTasksPage.ClientWidth;
  DeleteDataCheck.Caption := '同时删除我的账单、设置、图片和备份（默认保留，升级/重装不丢数据）';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\{#MyAppExeName}')) then
    Exec(ExpandConstant('{app}\{#MyAppExeName}'), '--request-shutdown', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    if DeleteDataCheck.Checked then
    begin
      if MsgBox('确定要删除全部账单、设置、图片和备份吗？此操作不可恢复！',
                mbConfirmation, MB_YESNO) = IDYES then
      begin
        DataDir := ExpandConstant('{localappdata}\BetterMoney');
        if DirExists(DataDir) then
          DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
