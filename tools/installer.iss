; Build with: ISCC.exe /DAppVersion=<x.y.z> tools\installer.iss
; tools\build.ps1 从 Cargo.toml 读取版本并传入 AppVersion。
; Produces dist\rclone_tray_setup.exe next to the portable executable.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceRoot
  #define SourceRoot "..\dist"
#endif

[Setup]
AppId={{35254699-8a21-4a87-98ad-c0490cf1862f}
AppName=rclone_tray
AppVersion={#AppVersion}
AppVerName=rclone_tray {#AppVersion}
AppPublisher=Benincasamatch
AppCopyright=MIT License
AppPublisherURL=https://github.com/Benincasamatch/Rclone_Tray
AppSupportURL=https://github.com/Benincasamatch/Rclone_Tray/issues
AppUpdatesURL=https://github.com/Benincasamatch/Rclone_Tray/releases
DefaultDirName={localappdata}\Programs\rclone_tray
DefaultGroupName=rclone_tray
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; The resident app owns this mutex; Inno then asks to close it before
; replacing files. Custom RCLONE_TRAY_DATA_DIR runs use a namespaced mutex.
AppMutex=Local\rclone_tray_single_instance
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=rclone_tray_setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\rclone_tray.exe
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription=rclone_tray installer
VersionInfoProductName=rclone_tray
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=Benincasamatch
VersionInfoCopyright=MIT License

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceRoot}\rclone_tray.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\rclone_tray.exe.sha256"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SourceRoot}\build-manifest.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SourceRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SourceRoot}\rclone\rclone.exe"; DestDir: "{app}\rclone"; Flags: ignoreversion

[Icons]
Name: "{group}\rclone_tray"; Filename: "{app}\rclone_tray.exe"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,rclone_tray}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\rclone_tray"; Filename: "{app}\rclone_tray.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\rclone_tray.exe"; Description: "{cm:LaunchProgram,rclone_tray}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Application data in %APPDATA%\rclone_tray is intentionally kept.
