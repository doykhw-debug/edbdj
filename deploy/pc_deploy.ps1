# 라디오 참여 도우미 → Oracle 서버(ssh haetsal) 첫 배포. 프로그램 폴더의 deploy_to_server.bat 을 더블클릭하면 실행된다.
# 1) 서버 연결 확인  2) PC 데이터 백업 + 서버로 보낼 묶음  3) .env·묶음·설치 스크립트를 scp 로 보냄
# 4) 서버 설치 (/opt/radio, .venv, systemd radio.service)  5) 로그인 비밀번호  6) 확인
# 방화벽(포트)은 건드리지 않는다. API 키는 화면에 나오지 않는다.
param(
  [string]$Server = "haetsal",
  [string]$HostName = "161.33.169.81",
  [string]$User = "ubuntu",
  [string]$KeyFile = (Join-Path $env:USERPROFILE ".ssh\haetsal-server.key"),
  [string]$Repo = "doykhw-debug/haetsal-radio",
  [string]$PublicUrl = "https://radio.haetsallab.com"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

function Step([string]$t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Done-Wait { Read-Host "Enter 를 누르면 닫습니다" | Out-Null }
function Fail([string]$t) { Write-Host ""; Write-Host "[실패] $t" -ForegroundColor Red; Done-Wait; exit 1 }

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Fail "프로그램 폴더에 .venv 가 없습니다. start_windows.bat 을 한 번 실행한 뒤 다시 하세요." }
if (-not (Get-Command ssh -ErrorAction SilentlyContinue) -or -not (Get-Command scp -ErrorAction SilentlyContinue)) {
  Fail "ssh/scp 가 없습니다. 윈도우 설정 → 시스템 → 선택적 기능 → 'OpenSSH 클라이언트'를 추가하세요."
}

function Test-Ssh {
  # 처음 접속하는 서버의 호스트 키는 저장하고(accept-new), 바뀐 키는 거부한다
  # (윈도우 PowerShell 5.1 은 Stop 상태에서 ssh 의 안내 글(stderr)을 오류로 보고 멈추므로 여기서만 Continue)
  $ErrorActionPreference = "Continue"
  $out = & ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new $Server "echo ok" 2>&1
  return @{ ok = ($LASTEXITCODE -eq 0); text = ($out | Out-String) }
}
function Protect-Key {
  # 윈도우 OpenSSH 는 다른 사용자도 읽을 수 있는 키를 거부한다 (UNPROTECTED PRIVATE KEY FILE) → 나만 읽게
  & icacls $KeyFile /inheritance:r | Out-Null
  & icacls $KeyFile /grant:r "$($env:USERNAME):(R)" | Out-Null
}

Step "1/6 서버 연결 확인 (ssh $Server → $User@$HostName)"
$sshDir = Join-Path $env:USERPROFILE ".ssh"
$sshConfig = Join-Path $sshDir "config"
$hasAlias = (Test-Path $sshConfig) -and (Select-String -Path $sshConfig -Pattern "^\s*Host\s+(.+\s)?$Server(\s|$)" -Quiet)
if (-not $hasAlias) {
  if (-not (Test-Path $KeyFile)) { Fail "서버 키 파일이 없습니다: $KeyFile" }
  New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
  if (Test-Path $sshConfig) { Copy-Item $sshConfig "$sshConfig.bak_radio" -Force }
  $entry = "`nHost $Server`n  HostName $HostName`n  User $User`n  IdentityFile `"$KeyFile`"`n  IdentitiesOnly yes`n  ServerAliveInterval 30`n"
  [IO.File]::AppendAllText($sshConfig, $entry, (New-Object System.Text.UTF8Encoding $false))
  Write-Host "  ssh 설정에 '$Server' ($User@$HostName, 키 $KeyFile) 를 추가했습니다: $sshConfig"
}
$try = Test-Ssh
if (-not $try.ok -and (Test-Path $KeyFile) -and $try.text -match "UNPROTECTED|bad permissions|too open") {
  Write-Host "  키 파일 권한이 넓어 ssh 가 거부했습니다. 나만 읽을 수 있게 바꾸고 다시 시도합니다."
  Protect-Key
  $try = Test-Ssh
}
if (-not $try.ok) {
  Write-Host $try.text
  Fail "ssh $Server 로 접속하지 못했습니다. 명령 프롬프트에서 'ssh $Server' 가 되는지 확인하세요."
}
Write-Host "  연결됨"

Step "2/6 PC 데이터 백업 + 서버로 보낼 묶음"
$stage = Join-Path $env:TEMP ("radio_deploy_" + (Get-Date -Format "yyyyMMddHHmmss"))
$out = & $py deploy\pc_prepare.py --env .env.server --stage $stage
if ($LASTEXITCODE -ne 0) { Fail "백업·묶음을 만들지 못했습니다. 위 내용을 캡처해 보내 주세요." }
$lines = @($out)
$lines | Select-Object -First ($lines.Count - 1) | ForEach-Object { Write-Host "  $_" }
$info = $lines[-1] | ConvertFrom-Json
if (-not $info.has_key) {
  Write-Host "  Claude API 키가 없습니다. 메모장이 열리면 ANTHROPIC_API_KEY= 뒤에 키를 붙여 넣고 저장한 뒤 메모장을 닫으세요." -ForegroundColor Yellow
  Start-Process notepad.exe -ArgumentList ".env.server" -Wait
}

Step "3/6 서버로 보내기 (scp → ~/radio_deploy, 나만 읽는 폴더)"
& ssh $Server 'mkdir -p ~/radio_deploy && chmod 700 ~/radio_deploy'
if ($LASTEXITCODE -ne 0) { Fail "서버에 임시 폴더를 만들지 못했습니다." }
& scp -q .env.server "${Server}:radio_deploy/radio.env"
if ($LASTEXITCODE -ne 0) { Fail ".env 를 보내지 못했습니다." }
# 윈도우 줄 끝(CRLF)이면 서버의 bash 가 못 읽으므로 LF 로 바꿔 보낸다
$setup = Join-Path $stage "server_setup.sh"
$text = [IO.File]::ReadAllText((Join-Path $Root "deploy\server_setup.sh")) -replace "`r`n", "`n"
[IO.File]::WriteAllText($setup, $text, (New-Object System.Text.UTF8Encoding $false))
& scp -q $setup "${Server}:radio_deploy/server_setup.sh"
if ($LASTEXITCODE -ne 0) { Fail "설치 스크립트를 보내지 못했습니다." }
$flag = ""
if ($info.package) {
  & scp -q $info.package "${Server}:radio_deploy/radio_data_server.zip"
  if ($LASTEXITCODE -ne 0) { Fail "데이터 묶음을 보내지 못했습니다." }
  $hasData = (& ssh $Server 'test -f /opt/radio/data/radio_helper.sqlite3 && echo yes || echo no' | Select-Object -Last 1)
  if ("$hasData".Trim() -eq "yes") {
    $answer = Read-Host "  서버에 이미 데이터가 있습니다. 이 PC 데이터로 바꿀까요? (서버 데이터는 옆에 보관) [y/N]"
    if ($answer -match '^(y|yes)$') { $flag = "--replace-data" }
  }
}
Write-Host "  보냄"

Step "4/6 서버 설치 (처음엔 5~10분: 패키지·브라우저 설치)"
$code = 3
for ($try = 1; $try -le 5 -and $code -eq 3; $try++) {
  & ssh -t $Server "bash ~/radio_deploy/server_setup.sh $Repo $flag"
  $code = $LASTEXITCODE
  if ($code -eq 3) {
    Write-Host ""
    Write-Host "  위 'ssh-ed25519 ...' 한 줄을 GitHub 저장소의 Deploy key 로 추가해야 합니다 (처음 한 번)." -ForegroundColor Yellow
    Write-Host "  브라우저가 열리면: Title 아무거나 → Key 칸에 붙여 넣기 → 'Allow write access' 는 체크하지 않음 → Add key"
    Start-Process "https://github.com/$Repo/settings/keys/new"
    Read-Host "  추가했으면 Enter (설치를 이어 갑니다)" | Out-Null
  }
}
if ($code -ne 0) { Fail "서버 설치가 끝나지 않았습니다 (코드 $code). 위 내용을 캡처해 보내 주세요." }

Step "5/6 로그인 비밀번호 (밖에서 열 때)"
$status = (& ssh $Server 'cd /opt/radio && .venv/bin/python -m radio_helper.remote status') -join "`n"
if ($status -match "REMOTE_LOGIN=on") {
  $answer = Read-Host "  PC 에서 쓰던 원격 비밀번호가 함께 옮겨졌습니다. 새로 정할까요? [y/N]"
  $setPassword = $answer -match '^(y|yes)$'
} else {
  Write-Host "  아직 비밀번호가 없습니다. 8자 이상으로 정하세요 (입력해도 화면에 안 보임)."
  $setPassword = $true
}
if ($setPassword) {
  & ssh -t $Server 'cd /opt/radio && .venv/bin/python -m radio_helper.remote set-password'
}

Step "6/6 확인"
# 서버 안에서: 관리 화면이 127.0.0.1:5002 에서 답하는지 (로그인 화면, HTTP 200)
$head = (& ssh $Server 'curl -sS -I -m 10 http://127.0.0.1:5002 | head -n 1') -join ""
Write-Host "  서버에서 curl -I http://127.0.0.1:5002 → $head"
if ($head -notmatch " 200") { Write-Host "  [주의] 200 이 아닙니다. 서버에서 'sudo journalctl -u radio -n 50' 으로 확인하세요." -ForegroundColor Yellow }
$bound = (& ssh $Server 'ss -ltn | grep :5002 || true') -join " "
if ($bound -match "0\.0\.0\.0:5002|\[::\]:5002|\*:5002") {
  Write-Host "  [주의] 5002번이 모든 주소에 열려 있습니다: $bound" -ForegroundColor Red
} elseif ($bound) {
  Write-Host "  127.0.0.1 에만 열림 (밖에서 직접 접속 불가): $($bound.Trim())"
}
if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
  $http = & curl.exe -s -o NUL -w "%{http_code}" --max-time 20 "$PublicUrl/health"
} else {
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $http = (Invoke-WebRequest -UseBasicParsing -Uri "$PublicUrl/health" -TimeoutSec 20).StatusCode
  } catch { $http = "" }
}
if ("$http" -eq "200") {
  Write-Host "  $PublicUrl 이(가) 열립니다 (HTTP 200). 브라우저에서 열고 비밀번호로 로그인하세요." -ForegroundColor Green
} else {
  Write-Host "  $PublicUrl 이(가) 아직 서버로 연결되지 않습니다 (HTTP $http)." -ForegroundColor Yellow
  Write-Host "  Cloudflare Zero Trust → Networks → Tunnels → 서버의 터널 → Public Hostname 에서"
  Write-Host "  radio.haetsallab.com → Service HTTP · 127.0.0.1:5002 로 정하세요 (PC 터널에 있던 같은 주소는 지움)."
}
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "PC 데이터 백업: $($info.full_backup)"
Write-Host "앞으로 서버 업데이트: ssh $Server 'bash /opt/radio/deploy.sh'"
Done-Wait
