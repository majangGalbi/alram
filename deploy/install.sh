#!/usr/bin/env bash
# alram 을 systemd 서비스로 등록하고 1분마다 자동 업데이트를 켭니다.
# 사용법 (저장소 폴더에서):  sudo deploy/install.sh [브랜치, 기본 main]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요: sudo deploy/install.sh"; exit 1; }

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${1:-main}"
USER_NAME="$(stat -c %U "$DIR")"

# Ubuntu 기본 설치에는 venv 모듈(ensurepip)이 없으므로 필요하면 python3-venv 설치
if ! python3 -c "import ensurepip" 2>/dev/null; then
  PYV="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  echo "python${PYV}-venv 설치"
  apt-get update -qq && apt-get install -y -qq "python${PYV}-venv"
fi
# 이전에 실패해서 pip 없이 반쯤 만들어진 가상환경은 다시 만듦
if [ -d "$DIR/.venv" ] && ! "$DIR/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  echo "불완전한 가상환경 삭제: $DIR/.venv"
  rm -rf "$DIR/.venv"
fi
if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "가상환경 생성: $DIR/.venv"
  runuser -u "$USER_NAME" -- python3 -m venv "$DIR/.venv"
fi
echo "패키지 설치"
runuser -u "$USER_NAME" -- "$DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$DIR/requirements.txt"

for f in alram.service alram-update.service alram-update.timer; do
  sed -e "s#__DIR__#$DIR#g" -e "s#__USER__#$USER_NAME#g" -e "s#__BRANCH__#$BRANCH#g" \
    "$DIR/deploy/$f" > "/etc/systemd/system/$f"
done
chmod +x "$DIR/deploy/auto_update.sh"
systemctl daemon-reload
systemctl enable --now alram.service
systemctl enable --now alram-update.timer
echo
echo "완료: 봇 서비스(alram)와 자동 업데이트($BRANCH 브랜치, 1분마다)가 켜졌습니다."
echo "  봇 로그:        journalctl -u alram -f"
echo "  업데이트 로그:  journalctl -u alram-update -f"
