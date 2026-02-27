#!/bin/bash
# VM 초기 설정 스크립트 (한 번만 실행)
set -e

echo "=== 1. SSH 배포 키 생성 ==="
if [ -f ~/.ssh/github_deploy ]; then
  echo "이미 존재함: ~/.ssh/github_deploy (스킵)"
else
  ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/github_deploy -N ""
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
echo "=== 2. 프로젝트 클론 ==="
if [ -d ~/kyusung02/.git ]; then
  echo "이미 클론됨: ~/kyusung02 (스킵)"
else
  git clone https://github.com/kyusung02/kyusung02.git ~/kyusung02
  echo "클론 완료"
fi

echo ""
echo "=== 3. Python 패키지 설치 ==="
pip3 install -r ~/kyusung02/requirements.txt --quiet
echo "완료"

echo ""
echo "=== 4. .env 파일 설정 ==="
if [ -f ~/kyusung02/.env ]; then
  echo "이미 존재함: ~/kyusung02/.env (스킵)"
else
  cat > ~/kyusung02/.env << 'ENVEOF'
INVEST_BOT_TOKEN=
INVEST_CHAT_ID=
DART_API_KEY=
GEMINI_API_KEY=
ENVEOF
  echo ".env 생성됨 → 아래 명령어로 API 키를 입력하세요:"
  echo "  nano ~/kyusung02/.env"
fi

echo ""
echo "=== 5. systemd 서비스 등록 ==="
sudo tee /etc/systemd/system/nemo-bot.service > /dev/null << SVCEOF
[Unit]
Description=Nemo Telegram Bot
After=network.target

[Service]
User=$USER
WorkingDirectory=/home/$USER/kyusung02
EnvironmentFile=/home/$USER/kyusung02/.env
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
echo "GitHub Secret [VM_SSH_KEY] 에 아래 내용을 붙여넣으세요:"
echo "=================================================="
cat ~/.ssh/github_deploy
echo "=================================================="
echo ""
echo "설정 완료! 다음 단계:"
echo "  1. .env 파일에 API 키 입력:  nano ~/kyusung02/.env"
echo "  2. 봇 시작:                  sudo systemctl start nemo-bot"
echo "  3. 상태 확인:                sudo systemctl status nemo-bot"
echo "  4. 실시간 로그:              journalctl -u nemo-bot -f"
