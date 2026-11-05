"""병합 후 모듈 간 상호작용 회귀 테스트 (디스코드 연결 없음)."""
import asyncio
import os
import sys
from datetime import timedelta

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import attendance  # noqa: E402
import bot  # noqa: E402
import polls  # noqa: E402
import schedule_calendar  # noqa: E402
from test_bot import make_interaction, make_member  # noqa: E402
from test_persistence import Restarter  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEOUL = pytz.timezone("Asia/Seoul")


def test_all_db_defaults_are_anchored_to_project_dir():
    # 실행 위치(cwd)가 달라도 모든 DB 파일이 jobs.sqlite 와 같은 폴더에 생겨야 함
    for path in (polls.DEFAULT_DB_PATH, attendance.DEFAULT_DB_PATH, schedule_calendar.DEFAULT_DB_PATH):
        assert os.path.isabs(path)
        assert os.path.dirname(path) == ROOT
    assert os.path.dirname(bot.DEFAULT_JOBS_DB_PATH) == ROOT


def test_db_files_are_distinct():
    names = {os.path.basename(p) for p in (polls.DEFAULT_DB_PATH, attendance.DEFAULT_DB_PATH,
                                           schedule_calendar.DEFAULT_DB_PATH, bot.DEFAULT_JOBS_DB_PATH)}
    assert len(names) == 4


def test_gitignore_covers_all_db_files():
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
        lines = {ln.strip() for ln in f}
    for name in ("jobs.sqlite", "polls.db", "attendance.db", "calendar.db"):
        assert name in lines, name


def test_command_names_are_unique_and_all_registered_before_sync():
    names = [c.name for c in bot.bot.tree.get_commands()]
    assert len(names) == len(set(names))
    expected = {"예약", "예약목록", "예약취소", "투표", "투표마감", "출석", "출석현황", "랭킹",
                "주사위", "뽑기", "팀나누기"}
    assert expected <= set(names)
    assert any(n.startswith("일정") or n == "달력" for n in names)


def test_intents_cover_module_requirements():
    intents = bot.bot.intents
    assert intents.guilds and intents.voice_states  # /팀나누기 는 음성 상태 필요


async def test_recurring_job_survives_restart(tmp_path, monkeypatch):
    r = Restarter("sqlite:///" + str(tmp_path / "jobs.sqlite"), monkeypatch)
    try:
        r.start()
        it = make_interaction()
        await bot.schedule_notification.callback(it, "매주 월수금 오전 9시", make_member(), "스탠드업")
        jobs = bot.bot.scheduler.get_jobs()
        assert len(jobs) == 1
        job_id, run_time = jobs[0].id, jobs[0].next_run_time
        assert jobs[0].kwargs["recurrence"]

        r.start()  # 재시작
        job = bot.bot.scheduler.get_job(job_id)
        assert job is not None
        assert job.func is bot.send_ping
        assert job.next_run_time == run_time
        assert job.kwargs["message"] == "스탠드업" and job.kwargs["recurrence"]
        # 재시작 후에도 다음 실행 시각 계산이 계속 됨 (반복)
        nxt = job.trigger.get_next_fire_time(run_time, run_time + timedelta(seconds=1))
        assert nxt is not None and nxt > run_time

        # 간격 반복도 복원
        await bot.schedule_notification.callback(make_interaction(), "2시간마다", make_member(), "물 마시기")
        r.start()
        assert len(bot.bot.scheduler.get_jobs()) == 2
        assert any(j.kwargs["message"] == "물 마시기" for j in bot.bot.scheduler.get_jobs())
    finally:
        r.stop()


async def test_close_shuts_down_background_tasks(tmp_path, monkeypatch):
    b = bot.MyBot()
    b.scheduler = bot.create_scheduler("sqlite:///" + str(tmp_path / "j.sqlite"))
    b.scheduler.start()
    manager = polls.PollManager(b, db_path=str(tmp_path / "p.db"))
    b.poll_manager = manager
    poll_task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    manager.tasks[1] = poll_task
    cal_task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    b._calendar_reminder_task = cal_task

    await b.close()
    await asyncio.sleep(0)
    assert not b.scheduler.running
    assert poll_task.cancelled() and cal_task.cancelled()
    assert manager.tasks == {}


async def test_close_without_setup_hook_is_safe():
    b = bot.MyBot()
    await b.close()  # 스케줄러 미시작 상태에서도 예외 없음
