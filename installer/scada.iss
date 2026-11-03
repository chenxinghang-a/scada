; ============================================================================
; SCADA 工业数据采集与监控系统 —— Inno Setup 安装脚本
;
; 构建：installer\build_installer.ps1（会自动传 AppVersion / SourceDir）
; 手动：ISCC.exe /DAppVersion=1.3.1091 /DSourceDir=..\dist-scada-1.3.1091 scada.iss
;
; 设计要点（每一条都是现场会遇到的）：
;   1. 安装日志默认开启（SetupLogging），出问题能拿到完整记录
;   2. 数据目录**不在安装目录**的默认删除范围内 —— 卸载时会问，默认「不删」
;   3. 卸载先停进程/服务再删文件，避免「文件被占用删不掉 → 残留」
;   4. 注册表只写必要项，且全部由 Inno 自动回收（uninsdeletekey）
;   5. 防火墙规则单独加/删（不依赖安装目录）
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
; 运行期数据目录（相对安装目录）。卸载时**询问**是否删除。
#define DataDirs "data,logs,exports,backups"

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
; 需要管理员（写 Program Files + 防火墙规则 + 可选服务）
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

[Languages]
; 简体中文语言包**随仓库提供**（installer/lang/ChineseSimplified.isl）。
;
; 为什么不写 `compiler:Languages\ChineseSimplified.isl`：
; Inno Setup 6.7.3 的 Languages 目录里**没有中文**（只有日/韩/德/法等），
; 官方把中文放在 `Unofficial/` 下，安装包不自带。写成 compiler: 会在
; 别人的机器上直接编译失败（"Couldn't open include file"）。
; 内置一份之后，任何装了 Inno 6.4+ 的机器都能直接构建。
Name: "chs"; MessagesFile: "lang\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "quicklaunchicon"; Description: "创建快速启动栏快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked
Name: "autostart"; Description: "开机自动启动（随 Windows 登录）"; GroupDescription: "附加任务:"; Flags: unchecked
Name: "firewall"; Description: "添加 Windows 防火墙入站规则（允许局域网访问 Web 界面）"; GroupDescription: "附加任务:"

[Files]
; 主程序目录（build.py 的产物）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 安装说明
Source: "..\启动说明.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
; `[Icons]` **不支持** `skipifsourcedoesntexist`（那是 [Files] 的 flag），
; 用它会让 ISCC 报「unknown flag」。改用 Check 判断文件存在。
; `.md` 不一定有文件关联，用记事本打开最稳；
; `shellexec` 是 [Run] 的 flag，[Icons] 里写它会让 ISCC 报「unknown flag」。
Name: "{group}\安装说明"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\启动说明.md"""; Check: FileExists(ExpandConstant('{app}\启动说明.md'))
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: quicklaunchicon

[Registry]
; 应用信息（卸载时自动删）
Root: HKA; Subkey: "Software\{#AppShortName}"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\{#AppShortName}"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"; Flags: uninsdeletekey
; 开机自启（当前用户；卸载时删）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppShortName}"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; 装完可选立即启动（默认勾选）
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
; 防火墙规则：只在勾选时加，且失败不阻断安装
; 端口来自 config.py：PORT=5000（模拟）/ REAL_PORT=5001（真实）
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppShortName} Web"" dir=in action=allow protocol=TCP localport=5000,5001 program=""{app}\{#AppExeName}"""; Flags: runhidden; Tasks: firewall; StatusMsg: "正在添加防火墙规则..."

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

  Msg := '是否同时删除运行数据？' + #13#10 + #13#10 +
         '包括：数据库（历史数据与报警记录）、运行日志、导出文件。' + #13#10 +
         '如果这台机器以后还要重装、或需要保留历史数据做追溯，请选「否」。' + #13#10 + #13#10 +
         '默认**不删除**（程序文件、快捷方式、注册表项无论如何都会清理干净）。';

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

  if Leftover <> '' then
    MsgBox('卸载完成。以下目录按你的选择**保留**了（里面是运行数据/配置）：' +
           Leftover + #13#10 + #13#10 +
           '确认不再需要后可以手动删除。', mbInformation, MB_OK);
end;
