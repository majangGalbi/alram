"""master 브랜치(v1.0) 봇의 예약을 새 저장소로 옮긴다.

v1.0 은 예약을 `scheduler.db`(APScheduler SQLAlchemyJobStore)에, 예약 주인을 `job_owners.json` 에 저장했다.
그 작업은 `__main__:send_ping(channel_id, user_mention, message, job_id)` 를 가리키므로 새 봇에서 그대로 실행할 수 없다.
봇 시작 시(setup_hook) 한 번 읽어서 같은 예약 번호(ID)로 새 형식(bot:send_ping + kwargs)으로 다시 등록하고,
원본 파일은 `*.migrated-날짜` 로 이름을 바꿔 다시 옮겨지지 않게 한다(삭제하지 않음).

파일 위치: 환경변수 LEGACY_SCHEDULER_DB / LEGACY_JOB_OWNERS, 기본은 bot.py 옆의 scheduler.db / job_owners.json.
"""
import json
import os
import pickle
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, List, Optional

import pytz

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEOUL = pytz.timezone("Asia/Seoul")
MENTION_RE = re.compile(r"<@!?(\d+)>")


def legacy_db_path() -> str:
    return os.getenv("LEGACY_SCHEDULER_DB") or os.path.join(BASE_DIR, "scheduler.db")


def legacy_owners_path() -> str:
    return os.getenv("LEGACY_JOB_OWNERS") or os.path.join(BASE_DIR, "job_owners.json")


@dataclass
class LegacyJob:
    id: str
    channel_id: int
    user_id: Optional[int]
    message: str
    target_name: str
    owner_id: Optional[int]
    trigger: object
    run_date: Optional[datetime]


def read_legacy_jobs(db_path: str, owners_path: str) -> List[LegacyJob]:
    """scheduler.db 의 작업 상태를 직접 읽는다(옛 send_ping 을 import 하지 않음)."""
    owners = {}
    if os.path.exists(owners_path):
        with open(owners_path, encoding="utf-8") as f:
            owners = json.load(f)

    conn = sqlite3.connect(db_path)
    try:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='apscheduler_jobs'").fetchone()
        rows = conn.execute("SELECT id, job_state FROM apscheduler_jobs").fetchall() if has_table else []
    finally:
        conn.close()

    jobs = []
    for job_id, blob in rows:
        state = pickle.loads(blob)
        args = list(state.get("args") or [])
        kwargs = dict(state.get("kwargs") or {})
        channel_id = kwargs.get("channel_id", args[0] if len(args) > 0 else None)
        mention = kwargs.get("user_mention", args[1] if len(args) > 1 else "")
        message = kwargs.get("message", args[2] if len(args) > 2 else "")
        m = MENTION_RE.search(str(mention or ""))
        name = state.get("name") or ""
        target_name = name.split(" | ", 1)[0] if " | " in name else ""
        owner = owners.get(job_id)
        trigger = state["trigger"]
        jobs.append(LegacyJob(
            id=job_id,
            channel_id=int(channel_id),
            user_id=int(m.group(1)) if m else None,
            message=str(message),
            target_name=target_name,
            owner_id=int(owner) if owner is not None else None,
            trigger=trigger,
            run_date=getattr(trigger, "run_date", None),
        ))
    return jobs


async def migrate_legacy_jobs(scheduler, func_ref: str, resolve_scope: Callable[[int], Awaitable[int]],
                              grace_seconds: int, now: Optional[datetime] = None,
                              db_path: Optional[str] = None, owners_path: Optional[str] = None) -> dict:
    """옛 예약을 옮기고 결과 개수를 돌려준다. 옛 DB 가 없으면 아무것도 하지 않음."""
    db_path = db_path or legacy_db_path()
    owners_path = owners_path or legacy_owners_path()
    result = {"migrated": 0, "expired": 0, "skipped": 0, "failed": 0}
    if not os.path.exists(db_path):
        return result

    now = now or datetime.now(SEOUL)
    try:
        jobs = read_legacy_jobs(db_path, owners_path)
    except Exception as e:  # 손상된 파일 등: 원본은 그대로 두고 봇은 정상 시작
        print(f"⚠️ 이전 버전 예약({db_path})을 읽지 못했습니다: {type(e).__name__}: {e}")
        result["failed"] += 1
        return result

    for job in jobs:
        if scheduler.get_job(job.id) is not None:
            result["skipped"] += 1
            continue
        if job.user_id is None:
            print(f"⚠️ 이전 예약 {job.id}: 대상 사용자를 알 수 없어 옮기지 않았습니다.")
            result["failed"] += 1
            continue
        if job.run_date is not None and job.run_date < now - timedelta(seconds=grace_seconds):
            print(f"⚠️ 이전 예약 {job.id}: 예정 시각({job.run_date})이 이미 지나 옮기지 않았습니다.")
            result["expired"] += 1
            continue
        try:
            scope = await resolve_scope(job.channel_id)
        except Exception:
            scope = job.channel_id
        kwargs = {
            "channel_id": job.channel_id,
            "user_id": job.user_id,
            "message": job.message,
            "scope": scope,
            "target_name": job.target_name,
        }
        if job.owner_id is not None:
            kwargs["owner_id"] = job.owner_id
        scheduler.add_job(func_ref, trigger=job.trigger, kwargs=kwargs, id=job.id,
                          name=f"{job.target_name} | {job.message}")
        result["migrated"] += 1

    stamp = now.strftime("%Y%m%d%H%M%S")
    for path in (db_path, owners_path):
        if os.path.exists(path):
            os.replace(path, f"{path}.migrated-{stamp}")
    print(f"📦 이전 버전 예약 이전: 옮김 {result['migrated']}건, 지난 예약 {result['expired']}건, "
          f"이미 있음 {result['skipped']}건, 실패 {result['failed']}건 (원본은 *.migrated-{stamp} 로 보관)")
    return result
