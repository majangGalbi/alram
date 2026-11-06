"""master(v1.0) 기능 이식 테스트: 본인 예약만 보기/취소, 드롭다운 취소, scheduler.db 예약 이전, /급식."""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import legacy_migration  # noqa: E402
import meal  # noqa: E402
from test_bot import make_interaction, scheduler  # noqa: E402,F401  (fixture 재사용)

SEOUL = pytz.timezone("Asia/Seoul")


def add(s, job_id, owner=None, scope=111, msg="hi", days=1):
    kwargs = {"channel_id": 1, "user_id": 2, "message": msg, "scope": scope, "target_name": "x"}
    if owner is not None:
        kwargs["owner_id"] = owner
    s.add_job(bot.send_ping, "date", run_date=datetime.now(SEOUL) + timedelta(days=days), id=job_id, kwargs=kwargs)


def listed_text(it):
    embed = it.response.send_message.call_args.kwargs["embed"]
    return str([(f.name, f.value) for f in embed.fields])


# ── 본인 예약만 ──────────────────────────────────────────────────────────────

async def test_list_shows_only_my_jobs_privately(scheduler):  # noqa: F811
    add(scheduler, "mine0001", owner=444, msg="내 예약")
    add(scheduler, "other001", owner=555, msg="남의 예약")
    add(scheduler, "legacy01", owner=None, msg="주인 없는 예약")
    it = make_interaction(user_id=444)
    await bot.list_jobs.callback(it)
    text = listed_text(it)
    assert "내 예약" in text and "주인 없는 예약" in text and "남의 예약" not in text
    assert it.response.send_message.call_args.kwargs["ephemeral"] is True
    assert it.response.send_message.call_args.kwargs["embed"].description == "총 2건"


async def test_cannot_cancel_someone_elses_job(scheduler):  # noqa: F811
    add(scheduler, "other001", owner=555)
    it = make_interaction(user_id=444)
    await bot.cancel_job.callback(it, "other001")
    assert scheduler.get_job("other001") is not None
    assert it.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_new_job_records_owner(scheduler):  # noqa: F811
    from test_bot import make_member, future_text
    await bot.schedule_notification.callback(make_interaction(user_id=777), future_text(), make_member(), "x")
    (job,) = scheduler.get_jobs()
    assert job.kwargs["owner_id"] == 777 and len(job.id) == 8


async def test_cancel_without_number_shows_my_jobs_dropdown(scheduler):  # noqa: F811
    add(scheduler, "mine0001", owner=444, msg="첫번째")
    add(scheduler, "mine0002", owner=444, msg="두번째", days=2)
    add(scheduler, "other001", owner=555)
    it = make_interaction(user_id=444)
    await bot.cancel_job.callback(it, None)
    kw = it.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True
    select = kw["view"].children[0]
    assert [o.value for o in select.options] == ["mine0001", "mine0002"]

    pick = make_interaction(user_id=444)
    pick.response.edit_message = AsyncMock()
    select._values = ["mine0001"]
    await select.callback(pick)
    assert scheduler.get_job("mine0001") is None and scheduler.get_job("mine0002") is not None
    assert pick.followup.send.call_args.kwargs["ephemeral"] is True


async def test_dropdown_rejects_other_user():
    view = bot.CancelView([], author_id=444)
    it = make_interaction(user_id=555)
    assert await view.interaction_check(it) is False


async def test_cancel_without_number_and_no_jobs(scheduler):  # noqa: F811
    it = make_interaction(user_id=444)
    await bot.cancel_job.callback(it, None)
    assert "없습니다" in it.response.send_message.call_args.args[0]


# ── v1.0 scheduler.db 이전 ───────────────────────────────────────────────────

async def _legacy_send_ping(channel_id, user_mention, message, job_id):  # v1.0 과 같은 시그니처
    pass


def make_legacy_db(tmp_path, monkeypatch, jobs):
    """v1.0 봇과 같은 방식(APScheduler SQLAlchemyJobStore + __main__:send_ping + args)으로 실제 DB 생성."""
    monkeypatch.setattr(sys.modules["__main__"], "send_ping", _legacy_send_ping, raising=False)
    db = tmp_path / "scheduler.db"
    store = SQLAlchemyJobStore(url=f"sqlite:///{db}")
    s = AsyncIOScheduler(jobstores={"default": store}, timezone="Asia/Seoul")
    s.start(paused=True)
    owners = {}
    for job_id, run_date, owner in jobs:
        s.add_job("__main__:send_ping", "date", run_date=run_date,
                  args=[222, "<@333>", f"메시지 {job_id}", job_id], id=job_id, name=f"마장 | 메시지 {job_id}")
        if owner:
            owners[job_id] = owner
    s.shutdown(wait=False)
    (tmp_path / "job_owners.json").write_text(json.dumps(owners), encoding="utf-8")
    return str(db), str(tmp_path / "job_owners.json")


async def test_migrates_v1_jobs_with_same_ids(tmp_path, monkeypatch):
    now = datetime.now(SEOUL)
    db, owners = make_legacy_db(tmp_path, monkeypatch, [
        ("9e3bbb67", now + timedelta(days=4), 444),
        ("e3d0fd70", now + timedelta(days=24), None),
        ("0ld00000", now - timedelta(hours=1), 444),
    ])
    sqlite3.connect(db).execute("SELECT count(*) FROM apscheduler_jobs").fetchone()
    new_db = f"sqlite:///{tmp_path / 'jobs.sqlite'}"
    s = bot.create_scheduler(new_db)
    s.start(paused=True)

    async def scope(channel_id):
        return 111

    result = await legacy_migration.migrate_legacy_jobs(s, bot.SEND_PING_REF, scope, bot.MISFIRE_GRACE_SECONDS,
                                                        db_path=db, owners_path=owners)
    assert result == {"migrated": 2, "expired": 1, "skipped": 0, "failed": 0}
    job = s.get_job("9e3bbb67")
    assert job.func_ref == bot.SEND_PING_REF
    assert job.kwargs == {"channel_id": 222, "user_id": 333, "message": "메시지 9e3bbb67",
                          "scope": 111, "target_name": "마장", "owner_id": 444}
    assert "owner_id" not in s.get_job("e3d0fd70").kwargs  # 주인 기록이 없던 예약
    s.shutdown(wait=False)

    # 원본은 보관용으로 이름만 바뀜 → 다음 시작 때 다시 옮기지 않음
    assert not os.path.exists(db) and not os.path.exists(owners)
    assert any(n.startswith("scheduler.db.migrated-") for n in os.listdir(tmp_path))

    # 새 DB 에 저장되어 재시작 후에도 남아 있음
    s2 = bot.create_scheduler(new_db)
    s2.start(paused=True)
    assert {j.id for j in s2.get_jobs()} == {"9e3bbb67", "e3d0fd70"}
    s2.shutdown(wait=False)


async def test_migration_noop_without_legacy_db(tmp_path):
    s = MagicMock()
    result = await legacy_migration.migrate_legacy_jobs(s, bot.SEND_PING_REF, AsyncMock(), 300,
                                                        db_path=str(tmp_path / "none.db"),
                                                        owners_path=str(tmp_path / "none.json"))
    assert result["migrated"] == 0
    s.add_job.assert_not_called()


async def test_migration_keeps_original_when_unreadable(tmp_path):
    bad = tmp_path / "scheduler.db"
    bad.write_bytes(b"not a database")
    result = await legacy_migration.migrate_legacy_jobs(MagicMock(), bot.SEND_PING_REF, AsyncMock(), 300,
                                                        db_path=str(bad), owners_path=str(tmp_path / "o.json"))
    assert result["failed"] == 1 and bad.exists()


# ── /급식 ────────────────────────────────────────────────────────────────────

SAMPLE = {"mealServiceDietInfo": [
    {"head": [{"list_total_count": 1}]},
    {"row": [{"DDISH_NM": "현미밥<br/>된장찌개 (5.6)<br/> 김치 ", "CAL_INFO": "812.3 Kcal"}]},
]}


def test_parse_meal_response():
    meal_row = meal.parse_meal_response(SAMPLE)
    assert meal_row["CAL_INFO"] == "812.3 Kcal"
    assert meal.menu_lines(meal_row) == "• 현미밥\n• 된장찌개 (5.6)\n• 김치"
    assert meal.parse_meal_response({"RESULT": {"CODE": "INFO-200"}}) is None
    assert meal.parse_meal_response(None) is None


async def test_meal_command_handles_api_error(monkeypatch):
    monkeypatch.setattr(meal, "fetch_meal", AsyncMock(side_effect=OSError("down")))
    it = make_interaction()
    await meal.meal_info.callback(it)
    assert "오류" in it.followup.send.call_args.args[0]


async def test_meal_command_sends_embed(monkeypatch):
    monkeypatch.setattr(meal, "fetch_meal", AsyncMock(return_value=SAMPLE["mealServiceDietInfo"][1]["row"][0]))
    it = make_interaction()
    await meal.meal_info.callback(it)
    embed = it.followup.send.call_args.kwargs["embed"]
    assert "중식" in embed.title and "현미밥" in embed.fields[0].value


def test_all_master_commands_registered():
    names = {c.name for c in bot.bot.tree.get_commands()}
    assert {"예약", "예약목록", "예약취소", "급식"} <= names
    assert bot.bot.get_command("sync") is not None
