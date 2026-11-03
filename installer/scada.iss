; ============================================================================
; SCADA 工业数据采集与监控系统 —— Inno Setup 安装脚本
;
; 构建：installer\build_installer.ps1（会自动传 AppVersion / SourceDir）
; 手动：ISCC.exe /DAppVersion=1.3.1091 /DSourceDir=..\dist-scada-1.3.1091 scada.iss
;
; 设计要点（每一条都是现场会遇到的）：
;   1. **安装向导第一步就能选语言**（English / 简体中文），选择会被记住
;   2. 安装日志默认开启（SetupLogging），出问题能拿到完整记录
;   3. 数据目录**不在安装目录**的默认删除范围内 —— 卸载时会问，默认「不删」
;   4. 卸载先停进程再删文件，避免「文件被占用删不掉 → 残留」
;   5. 注册表只写必要项，且全部由 Inno 自动回收（uninsdeletekey）
;   6. 防火墙规则单独加/删（不依赖安装目录）
;   7. 自定义文案全部走 [CustomMessages] 双语，界面不出现「中英混杂」
; ============================================================================

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist-scada-" + AppVersion
#endif

#define AppName "SCADA 工业监控系统"
#define AppShortName "SCADA"
#define AppPublisher "SCADA"
#define AppExeName "SCADA.exe"

[Setup]
AppId={{8F3C2A91-7B4E-4D2A-9C15-3E8A6D5F1B27}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppShortName}
; 「应用和功能」里显示**带版本**的名字 —— 现场同时装了多个版本时
; 才能分清该卸哪个。写成 {#AppName}（不带版本）会让两个版本看起来一样。
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} 安装程序
OutputDir=..\dist-installer
OutputBaseFilename={#AppShortName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装日志：写到 %TEMP%\Setup Log <时间>.txt，也可 /LOG="路径" 指定
SetupLogging=yes
; 需要管理员（写 Program Files + 防火墙规则）
PrivilegesRequired=admin
; 允许用 `/CURRENTUSER` 以普通用户安装（装到 %LOCALAPPDATA%，注册表落 HKCU）。
; 现场常见「IT 统一装」和「操作员自己装」两种场景，都要支持；
; 也给自动化验证留了不需要 UAC 的路径（CI 跑不起提权）。
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesInstallIn64BitMode=x64compatible
; 覆盖安装时先关掉正在运行的程序（否则文件占用 → 安装失败）
CloseApplications=yes
RestartApplications=no
; 允许用户在向导里改路径
DisableDirPage=no
DisableProgramGroupPage=no
; 卸载时不要删掉用户自己放进去的文件 —— 删除范围在 [UninstallDelete] 与 [Code] 里显式控制

; ---------------------------------------------------------------------------
; 语言：**两条以上才会出现「选择语言」页**，且是向导第一步。
;
; 英文用 Inno 自带的 Default.isl；中文是仓库内置的
; （Inno 6.7.3 的安装目录里**没有**中文，官方放在 Unofficial/ 下，
;   写 compiler:Languages\ChineseSimplified.isl 会在别人机器上编译失败）。
;
; 顺序即默认选中项：英文在前 → 非中文环境默认英文；中文用户第一页就能切。
; 选过的语言会记在注册表，下次升级/卸载沿用同一种语言。
; ---------------------------------------------------------------------------
[Languages]
Name: "en";  MessagesFile: "compiler:Default.isl"
Name: "chs"; MessagesFile: "lang\ChineseSimplified.isl"

; ---------------------------------------------------------------------------
; 自定义文案（安装包自己加的文字，Inno 内置消息表覆盖不到）。
; 每条都要给 en. 和 chs. 两份 —— 漏一条就会在英文界面里冒出中文。
; ---------------------------------------------------------------------------
[CustomMessages]
en.TaskDesktopIcon=Create a &desktop shortcut
en.TaskQuickLaunch=Create a &Quick Launch shortcut
en.TaskAutostart=Start automatically when Windows &starts
en.TaskFirewall=Add Windows Firewall inbound rule (allow LAN access to the web UI)
en.GroupTasks=Additional tasks:
en.RunApp=Launch SCADA now
en.StatusFirewall=Adding firewall rules...
en.AskDeleteData=Delete runtime data as well?%n%nThis includes the database (history and alarm records), logs and exported files.%nIf you plan to reinstall on this machine, or need the history for auditing, choose No.%n%nDefault is No (program files, shortcuts and registry entries are always removed).
en.LeftoverReport=Uninstall finished. The following folders were KEPT (runtime data / configuration):%n%n%s%n%nDelete them manually once you are sure they are no longer needed.

chs.TaskDesktopIcon=创建桌面快捷方式
chs.TaskQuickLaunch=创建快速启动栏快捷方式
chs.TaskAutostart=开机自动启动（随 Windows 登录）
chs.TaskFirewall=添加 Windows 防火墙入站规则（允许局域网访问 Web 界面）
chs.GroupTasks=附加任务:
chs.RunApp=立即启动 SCADA
chs.StatusFirewall=正在添加防火墙规则...
chs.AskDeleteData=是否同时删除运行数据？%n%n包括：数据库（历史数据与报警记录）、运行日志、导出文件。%n如果这台机器以后还要重装、或需要保留历史数据做追溯，请选「否」。%n%n默认**不删除**（程序文件、快捷方式、注册表项无论如何都会清理干净）。
chs.LeftoverReport=卸载完成。以下目录按你的选择**保留**了（里面是运行数据/配置）：%n%n%s%n%n确认不再需要后可以手动删除。

[Tasks]
Name: "desktopicon"; Description: "{cm:TaskDesktopIcon}"; GroupDescription: "{cm:GroupTasks}"
Name: "quicklaunchicon"; Description: "{cm:TaskQuickLaunch}"; GroupDescription: "{cm:GroupTasks}"; Flags: unchecked
Name: "autostart"; Description: "{cm:TaskAutostart}"; GroupDescription: "{cm:GroupTasks}"; Flags: unchecked
Name: "firewall"; Description: "{cm:TaskFirewall}"; GroupDescription: "{cm:GroupTasks}"

[Files]
; 主程序目录（build.py 的产物）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 安装说明
Source: "..\启动说明.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
; `.md` 不一定有文件关联，用记事本打开最稳；
; `shellexec` 是 [Run] 的 flag，[Icons] 里写它会让 ISCC 报「unknown flag」。
Name: "{group}\安装说明"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\启动说明.md"""; Check: FileExists(ExpandConstant('{app}\启动说明.md'))
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: quicklaunchicon

[Registry]
; 应用信息（卸载时自动删）。HKA = 自动跟随安装模式（管理员→HKLM，普通用户→HKCU）
Root: HKA; Subkey: "Software\{#AppShortName}"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\{#AppShortName}"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"; Flags: uninsdeletekey
; 记住安装时选的语言，下次升级/卸载沿用（应用启动时也会读它做默认界面语言）
Root: HKA; Subkey: "Software\{#AppShortName}"; ValueType: string; ValueName: "InstallerLanguage"; ValueData: "{language}"; Flags: uninsdeletekey
; 开机自启（当前用户；卸载时删）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppShortName}"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:RunApp}"; Flags: nowait postinstall skipifsilent
; 端口来自 config.py：PORT=5000（模拟）/ REAL_PORT=5001（真实）
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppShortName} Web"" dir=in action=allow protocol=TCP localport=5000,5001 program=""{app}\{#AppExeName}"""; Flags: runhidden; Tasks: firewall; StatusMsg: "{cm:StatusFirewall}"

[UninstallRun]
; 1) 先停进程（TaskKill 是幂等的：没在跑也返回 0）
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExeName} /T"; Flags: runhidden; RunOnceId: "KillApp"
; 2) 再删防火墙规则（规则不存在时 netsh 返回非 0，但 Inno 不因此中断卸载）
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppShortName} Web"""; Flags: runhidden; RunOnceId: "DelFwRule"

[UninstallDelete]
; 只删**确定是缓存/临时**的东西。用户数据在 [Code] 里问过才删。
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\**\__pycache__"
Type: filesandordirs; Name: "{app}\.pytest_cache"
Type: files; Name: "{app}\*.pyc"
Type: files; Name: "{app}\stderr.txt"
Type: files; Name: "{app}\stdout.txt"
Type: filesandordirs; Name: "{app}\.checkup_probe"
Type: dirifempty; Name: "{app}"

[Code]
var
  DeleteDataOnUninstall: Boolean;

{ 注：不在 [Code] 里自己检测进程 —— `CloseApplications=yes` 已让 Inno
  在覆盖安装前自动检测占用并提示用户关闭，比手写 tasklist 解析更可靠
  （Inno 的 Exec 拿不到子进程输出，手写只能靠退出码，会误判）。 }

{ ---------- 卸载前：确认 + 记录数据删除选择 ---------- }
function InitializeUninstall(): Boolean;
var
  DataFound: Boolean;
  Msg: String;
begin
  Result := True;

  { 有数据目录才问，避免「装完没跑过」也弹窗 }
  DataFound := DirExists(ExpandConstant('{app}\data')) or
               DirExists(ExpandConstant('{app}\logs')) or
               DirExists(ExpandConstant('{app}\exports'));

  if not DataFound then
  begin
    DeleteDataOnUninstall := False;
    Exit;
  end;

  { 静默卸载（/SILENT、/VERYSILENT）下**不能弹窗** —— 否则自动化脚本会卡死。
    静默模式的默认行为是「保留数据」（安全方向），要删数据必须显式传
    `/DELETE-DATA`：unins000.exe /SILENT /DELETE-DATA }
  if UninstallSilent then
  begin
    DeleteDataOnUninstall := Pos('/DELETE-DATA', GetCmdTail()) > 0;
    Exit;
  end;

  { 文案走 CustomMessages —— 卸载程序会沿用安装时选的语言 }
  Msg := ExpandConstant('{cm:AskDeleteData}');

  { 默认按钮是「否」—— 误删现场数据的代价远高于多留一个目录 }
  if MsgBox(Msg, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DeleteDataOnUninstall := True
  else
    DeleteDataOnUninstall := False;
end;

{ ---------- 卸载后：按选择删数据，并报告残留 ---------- }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
  i: Integer;
  Leftover: String;
  Names: array[0..3] of String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  AppDir := ExpandConstant('{app}');

  if DeleteDataOnUninstall then
  begin
    Names[0] := 'data';
    Names[1] := 'logs';
    Names[2] := 'exports';
    Names[3] := 'backups';
    for i := 0 to 3 do
      DelTree(AppDir + '\' + Names[i], True, True, True);
  end;

  { 报告残留 —— 卸载完不能「看起来干净、其实留了一堆」 }
  Leftover := '';
  Names[0] := 'data';
  Names[1] := 'logs';
  Names[2] := 'exports';
  Names[3] := '配置';
  for i := 0 to 3 do
    if DirExists(AppDir + '\' + Names[i]) then
      Leftover := Leftover + #13#10 + '  ' + AppDir + '\' + Names[i];

  { 静默卸载下**不能弹窗** —— 否则 /VERYSILENT 会卡在这个 MessageBox 上，
    自动化（CI、批量部署）永远等不到进程退出。
    实测踩到：`-DeleteData` 时残留为空不弹，不加时残留非空就卡死。
    静默模式改成写卸载日志，管理员事后可从 unins*.log 里查到。 }
  if Leftover <> '' then
  begin
    if UninstallSilent then
      Log('保留的运行数据目录（未删除）:' + Leftover)
    else
      MsgBox(FmtMessage(ExpandConstant('{cm:LeftoverReport}'), [Leftover]),
             mbInformation, MB_OK);
  end;
end;