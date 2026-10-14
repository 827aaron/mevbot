; 詹姆斯智能体 / James Agent —— Windows 安装程序（Inno Setup 6）
; 在 GitHub Actions 上：先用 PyInstaller 打出 dist\JamesAgent\，再执行
;   iscc /DAppVersion=0.2.0 packaging\windows\JamesAgent.iss
; 不需要管理员权限：装到当前用户的 %LOCALAPPDATA%\Programs\JamesAgent；数据在 %APPDATA%\JamesAgent（卸载时保留）。
; 未做代码签名（第 5a 测试版）：第一次运行会被 SmartScreen 拦下 → 「更多信息」→「仍要运行」。

#ifndef AppVersion
  #define AppVersion "0.2.3"
#endif
#define AppNameZh "詹姆斯智能体"
#define AppExe "JamesAgent.exe"

[Setup]
AppId={{6E0D8A52-4C3B-4E7B-9A51-3B9C0F2A7D11}
AppName={#AppNameZh}
AppVerName={#AppNameZh} {#AppVersion}
AppVersion={#AppVersion}
AppPublisher=James Agent
DefaultDirName={localappdata}\Programs\JamesAgent
DefaultGroupName={#AppNameZh}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=JamesAgent-Setup-{#AppVersion}
SetupIconFile=..\icons\app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\JamesAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppNameZh}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppNameZh}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppNameZh}}"; Flags: nowait postinstall skipifsilent
