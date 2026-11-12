; Установщик Berkut VTT: ставится без прав администратора, данные лежат
; в папке data рядом с .exe. При обновлении data не трогается; при удалении
; сохраняется по умолчанию, а удаляется только по явному согласию
; (или ключом /DELETEUSERDATA для автоматических проверок).
#define MyAppName "Berkut VTT"
#define MyAppVersion "2.1 beta"
#define MyAppPublisher "Berkut"
#define MyAppExeName "Berkut VTT.exe"
#define StageDir "..\build\dist\Berkut VTT beta 2.1"

[Setup]
AppId={{8C4C2D0C-6B0E-4A2F-9BD0-9B0E6B7A1E31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=G:\001VTT\mvp\Berkut beta 2.1
OutputBaseFilename=Berkut VTT Setup 2.1 beta
SetupIconFile=..\client\assets\icons\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
; Показывать выбор папки всегда: и при первой установке, и при обновлении.
; UsePreviousAppDir оставляет прошлый путь по умолчанию, но его можно изменить.
DisableDirPage=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
VersionInfoVersion=2.1.0.0
VersionInfoDescription={#MyAppName} {#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion=2.1.0.0

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; data исключена: установщик не должен затирать миры и автосохранения.
Source: "{#StageDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "data\*"

[Dirs]
; Папка данных создаётся при установке и переживает обновление.
; При удалении её судьбу решает InitializeUninstall.
Name: "{app}\data"; Flags: uninsneveruninstall

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[Code]
var
  DeleteUserData: Boolean;

function DataDirHasFiles(): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if FindFirst(ExpandConstant('{app}\data\*'), FindRec) then
  try
    repeat
      if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
      begin
        Result := True;
        Exit;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

function HasDeleteUserDataSwitch(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/DELETEUSERDATA') = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

function InitializeUninstall(): Boolean;
var
  Answer: Integer;
begin
  DeleteUserData := HasDeleteUserDataSwitch();
  Result := True;
  if DeleteUserData then
    Exit;
  if DataDirHasFiles() then
  begin
    if UninstallSilent then
      Answer := IDNO
    else
      Answer := MsgBox(
        'Удалить сохранения, миры и автосохранения?' + #13#10 +
        'Папка данных: ' + ExpandConstant('{app}\data') + #13#10#13#10 +
        'Если ответить «Нет», данные останутся на диске.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
    DeleteUserData := Answer = IDYES;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usUninstall) and DeleteUserData then
    DelTree(ExpandConstant('{app}\data'), True, True, True);
end;
