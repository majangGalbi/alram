# alram 전체 코드 리뷰 결과 (claude/max-review-fixes)

라인 번호는 **main 브랜치의 `bot.py` 기준**입니다. 모든 항목은 코드/실행으로 재확인했습니다
(검증 환경: Python 3.13, discord.py 2.7.1, APScheduler 3.11.3, dateparser 1.5.0).

## 수정한 문제 (심각도순)

| # | 심각도 | 위치 | 문제 | 수정 |
|---|---|---|---|---|
| 1 | 높음 | bot.py:36 | APScheduler 3.x `misfire_grace_time` 기본값이 1초 → 실행이 1초만 늦어도 알림이 조용히 누락 | 300초 + `coalesce=True` |
| 2 | 높음(보안) | bot.py:48, 114 | 사용자 입력 메시지의 `@everyone`/`@here`/역할 멘션이 봇 권한으로 그대로 발송 | 봇 전역 `AllowedMentions(everyone=False, roles=False)`, 알림은 대상 사용자만 멘션 허용 |
| 3 | 높음(보안) | bot.py:125, 154 | `/예약목록`이 **모든 서버**의 예약(대상·내용)을 노출, `/예약취소`는 다른 서버 예약도 4자리 ID로 취소 가능 | 예약에 서버(DM이면 채널) 범위 저장, 목록/취소를 현재 범위로 제한 |
| 4 | 중간 | bot.py:98-107 | 4자리 ID 충돌 시 `ConflictingIdError` → 처리 안 된 예외로 응답이 "생각 중..."에 멈춤 | 기존 ID와 겹치지 않을 때까지 재생성 |
| 5 | 중간 | bot.py:24 | 포트 8080 하드코딩 — Procfile `web` 프로세스는 `$PORT`에 바인딩해야 함 | `PORT` 환경변수 우선 |
| 6 | 중간 | bot.py:27, 167-168 | 웹서버 스레드가 non-daemon → 토큰 오류 등으로 봇이 죽어도 프로세스가 살아 있음. 토큰 미설정 시 불명확한 오류. import만 해도 봇이 실행됨(테스트 불가) | `daemon=True`, 토큰 검사, `if __name__ == "__main__"` |
| 7 | 중간 | bot.py:45-49 | 캐시에 없는 채널(`get_channel`→None)이면 알림이 아무 로그 없이 버려짐, 전송 예외도 로그 없음 | `fetch_channel` 대체 조회, 실패 시 로그 |
| 8 | 중간 | bot.py:133-148 | 예약 25개 초과 또는 필드값 1024자 초과 시 임베드 전송 실패(디스코드 제한) | 25개까지 표시 + 푸터, 필드값 잘라냄 |
| 9 | 중간 | bot.py:48 | 메시지 길이 제한 없음 → 2000자 초과 시 알림 시점에 전송 실패 | 1500자 초과 예약 거부 |
| 10 | 낮음 | 전역 | 슬래시 명령 예외 시 사용자 응답 없음 | `bot.tree.error` 핸들러 |
| 11 | 낮음 | bot.py:110-112, 139-140 | `%p`는 로케일 의존(AM/PM 치환 실패 가능) | 시각으로 직접 오전/오후 결정 |
| 12 | 낮음 | bot.py:135 | 표시 이름에 `" \| "`가 있으면 대상/내용 분리 오류 | 작업 kwargs에 구조화해 저장 |
| 13 | 낮음 | requirements.txt | APScheduler 버전 미고정(4.x는 `AsyncIOScheduler` API가 다름) | `APScheduler>=3.6,<4` |
| 14 | 낮음 | bot.py:154 | 취소 번호 앞뒤 공백/대문자 입력 시 찾지 못함 | `strip().lower()` |

## 수정하지 않은 문제 — 시간 파싱 영역 (claude/time-parsing-overhaul 에서 재작성 중)

기준 시각 2026-11-05 16:00(KST)으로 `dateparser.parse(..., languages=['ko'], settings={'RELATIVE_BASE': ...})` 실제 실행 결과입니다.

| # | 심각도 | 위치 | 문제 (실측) |
|---|---|---|---|
| P1 | 높음 | bot.py:71-75 | `내일 오후 3시` → `2026-11-06 16:00`, `오늘 오후 8시` → `2026-11-05 16:00`, `내일 오전 11시 30분` → `2026-11-06 16:00`: 상대 날짜와 시각을 함께 쓰면 **시각이 무시되고 현재 시각**으로 예약됨. 오류 안내문(81-84줄)의 예시 자체가 잘못 동작 |
| P2 | 높음 | bot.py:54, 71 | 설명에 예시로 든 `10분 뒤` → `None`(파싱 실패). `10분 후`는 정상 |
| P3 | 중간 | bot.py:90 | 입력에 시간대가 포함되면(`2026-11-06 09:00 KST`) dateparser가 aware datetime 반환 → `localize`가 `ValueError: Not naive datetime` 발생 |
| P4 | 중간 | bot.py:71-75, 93 | `오후 3시`(현재 16시) → 오늘 15:00, `5월 23일` → 2026-05-23 00:00 처럼 연/일 생략 시 과거로 해석되어 "이전 시간" 오류. `PREFER_DATES_FROM: 'future'` 미설정 |
| P5 | 낮음 | bot.py:64, 79-95 | `defer(ephemeral=False)` 후 첫 followup은 지연 응답을 대체하므로 `ephemeral=True` 오류 메시지가 공개로 표시됨 (Discord API 동작, 실서버 미검증) |

## 그 밖에 수정하지 않은 사항
- 예약이 메모리(MemoryJobStore)에만 저장되어 재시작/재배포 시 모두 사라짐 (설계 변경 필요: SQLAlchemyJobStore 등).
- `setup_hook`에서 매 시작마다 전역 `tree.sync()` 호출 (레이트리밋 가능성, 낮음).
- Flask 개발 서버를 운영에 사용 (keep-alive 용도라 영향 작음).

## 테스트
`pip install -r requirements-dev.txt && pytest` — `tests/test_bot.py` (디스코드 연결 없이 mock).
