#!/bin/bash
# VM 초기 설정 스크립트 (한 번만 실행)
set -e

echo "=== 1. SSH 배포 키 생성 ==="
if [ -f ~/.ssh/github_deploy ]; then
  echo "이미 존재함: ~/.ssh/github_deploy (스킵)"
else
  ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/github_deploy -N ""
  chmod 600 ~/.ssh/github_deploy
  chmod 644 ~/.ssh/github_deploy.pub
  echo "키 생성 완료"
fi

echo ""
echo "=== authorized_keys에 공개키 추가 ==="
mkdir -p ~/.ssh
chmod 700 ~/.ssh
grep -qF "$(cat ~/.ssh/github_deploy.pub)" ~/.ssh/authorized_keys 2>/dev/null || \
  cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo "완료"

echo ""
echo "=== 2. SSH 설정 (GitHub deploy key 사용) ==="
# 이 host alias로 git을 호출하면 deploy key를 사용한다.
if ! grep -q "Host github-nemo-bot" ~/.ssh/config 2>/dev/null; then
  cat >> ~/.ssh/config <<'SSHCFG'

Host github-nemo-bot
    HostName github.com
    User git
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
SSHCFG
  chmod 600 ~/.ssh/config
  echo "SSH config에 github-nemo-bot host alias 추가"
fi

echo ""
echo "=== 3. 프로젝트 클론 ==="
if [ -d ~/kyusung02/.git ]; then
  echo "이미 클론됨: ~/kyusung02 (스킵)"
else
  # HTTPS 대신 SSH로 클론하여 deploy key를 활용한다 (MITM 방어 + 인증).
  # 사전에 GitHub Settings → Deploy keys 에 ~/.ssh/github_deploy.pub 등록 필요.
  ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null
  git clone git@github-nemo-bot:kyusung02/kyusung02.git ~/kyusung02
  echo "클론 완료"
fi

echo ""
echo "=== 4. Python 패키지 설치 ==="
pip3 install -r ~/kyusung02/requirements.txt --quiet
echo "완료"

echo ""
echo "=== 5. .env 파일 설정 ==="
if [ -f ~/kyusung02/.env ]; then
  echo "이미 존재함: ~/kyusung02/.env (스킵)"
else
  cat > ~/kyusung02/.env << 'ENVEOF'
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_BOT_TOKEN=
TELEGRAM_USER_SESSION=
TELEGRAM_BOT_SESSION=
MY_TELEGRAM_ID=
DART_API_KEY=
GEMINI_API_KEY=
WATCH_CHANNELS=
ENVEOF
  echo ".env 생성됨 → 아래 명령어로 API 키를 입력하세요:"
  echo "  nano ~/kyusung02/.env"
fi
# 환경변수 파일은 항상 0600 (다른 OS 사용자가 읽지 못하도록)
chmod 600 ~/kyusung02/.env

echo ""
echo "=== 6. systemd 서비스 등록 ==="
sudo tee /etc/systemd/system/nemo-bot.service > /dev/null << SVCEOF
[Unit]
Description=Nemo Telegram Bot
After=network.target

[Service]
User=$USER
WorkingDirectory=/home/$USER/kyusung02
EnvironmentFile=/home/$USER/kyusung02/.env
Environment=TZ=Asia/Seoul
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable nemo-bot
echo "서비스 등록 완료 (nemo-bot)"

echo ""
echo "=================================================="
echo "다음 단계:"
echo "  1. GitHub deploy key 등록:"
echo "       Settings → Deploy keys → Add deploy key"
echo "       공개키 경로: ~/.ssh/github_deploy.pub"
echo "     (보안상 이 스크립트는 공개키 내용을 출력하지 않습니다."
echo "      직접 'cat ~/.ssh/github_deploy.pub' 명령으로 확인해 등록하세요.)"
echo "  2. .env 파일에 API 키 입력:  nano ~/kyusung02/.env"
echo "  3. 봇 시작:                  sudo systemctl start nemo-bot"
echo "  4. 상태 확인:                sudo systemctl status nemo-bot"
echo "  5. 실시간 로그:              journalctl -u nemo-bot -f"
echo "=================================================="
