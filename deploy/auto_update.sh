#!/usr/bin/env bash
# alram 자동 업데이트 (systemd 타이머가 1분마다 실행)
#   GitHub에 새 커밋이 있으면 받아서 봇을 재시작하고,
#   재시작 후 봇이 죽으면 이전 버전으로 자동으로 되돌립니다.
# 수동 실행: sudo /경로/alram/deploy/auto_update.sh
set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${BRANCH:-main}"                    # 따라갈 브랜치
SERVICE="${SERVICE:-alram}"                 # 봇 systemd 서비스 이름
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
HEALTH_WAIT="${HEALTH_WAIT:-30}"            # 재시작 후 정상 동작 확인까지 기다릴 초
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

log() { echo "[$(date '+%F %T')] $*"; }

# 동시에 두 번 돌지 않도록 잠금
exec 9>"/tmp/alram-update.lock"
flock -n 9 || exit 0

cd "$APP_DIR" || { log "폴더 없음: $APP_DIR"; exit 1; }
OWNER="$(stat -c %U "$APP_DIR")"
# git/pip 는 저장소 주인 계정으로 실행 (root 소유 파일이 생기지 않게)
as_owner() { if [ "$(id -un)" = "$OWNER" ]; then "$@"; else runuser -u "$OWNER" -- "$@"; fi; }

as_owner git fetch --quiet origin "$BRANCH" || { log "git fetch 실패 (네트워크?)"; exit 1; }
OLD="$(as_owner git rev-parse HEAD)"
NEW="$(as_owner git rev-parse "origin/$BRANCH")"
[ "$OLD" = "$NEW" ] && exit 0

# 실패해서 되돌린 커밋은 다시 시도하지 않음 (1분마다 재시작 반복 방지). 새 커밋이 올라오면 다시 시도.
BAD_FILE="$APP_DIR/.git/alram-bad-commit"
if [ -f "$BAD_FILE" ] && [ "$(cat "$BAD_FILE")" = "$NEW" ]; then exit 0; fi

log "새 커밋 발견: ${OLD:0:7} -> ${NEW:0:7} ($BRANCH)"
if ! as_owner git merge --ff-only --quiet "origin/$BRANCH"; then
  log "업데이트 중단: 서버에서 직접 고친 파일이 있거나 브랜치가 갈라졌습니다. 'git status' 로 확인하세요."
  exit 1
fi

install_requirements() {
  as_owner "$PYTHON" -m pip install --quiet --disable-pip-version-check -r requirements.txt
}

rollback() {
  log "이전 버전(${OLD:0:7})으로 되돌립니다. ${NEW:0:7} 은(는) 다음 커밋이 올라올 때까지 건너뜁니다."
  echo "$NEW" > "$BAD_FILE"
  as_owner git reset --hard --quiet "$OLD"
  if ! as_owner git diff --quiet "$OLD" "$NEW" -- requirements.txt; then install_requirements; fi
  "$SYSTEMCTL" restart "$SERVICE"
}

if ! as_owner git diff --quiet "$OLD" "$NEW" -- requirements.txt; then
  log "requirements.txt 변경 → 패키지 설치"
  if ! install_requirements; then log "패키지 설치 실패"; rollback; exit 1; fi
fi

"$SYSTEMCTL" restart "$SERVICE"
sleep "$HEALTH_WAIT"
if "$SYSTEMCTL" is-active --quiet "$SERVICE"; then
  log "업데이트 완료: ${NEW:0:7} 실행 중"
else
  log "업데이트 후 봇이 실행되지 않습니다 (journalctl -u $SERVICE 확인)"
  rollback
  exit 1
fi
