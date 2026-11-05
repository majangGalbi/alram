"""예약 영구 저장(SQLite) 회귀 테스트: 임시 SQLite 파일로 봇 재시작을 흉내 냄 (디스코드 연결 없음)."""
import asyncio
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
from test_bot import make_interaction, make_member, future_text  # noqa: E402

SEOUL = pytz.timezone("Asia/Seoul")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db_url(tmp_path):
    return "sqlite:///" + str(tmp_path / "jobs.sqlite")


class Restarter:
    """같은 DB 파일로 스케줄러를 끄고 다시 켜서 봇 재시작/재배포를 흉내 냄."""

    def __init__(self, db_url, monkeypatch):
        self.db_url = db_url
        self.monkeypatch = monkeypatch
        self.current = None

    def start(self, paused=False):
        self.stop()
        s = bot.create_scheduler(self.db_url)
        s.add_listener(bot.log_missed_job, bot.EVENT_JOB_MISSED)
        s.start(paused=paused)
        self.monkeypatch.setattr(bot.bot, "scheduler", s)
        self.current = s
        return s

    def stop(self):
        if self.current is not None and self.current.running:
            self.current.shutdown(wait=False)
        self.current = None


@pytest.fixture
async def restarter(db_url, monkeypatch):
    r = Restarter(db_url, monkeypatch)
    yield r
    r.stop()


def store_job(scheduler, job_id, run_date, scope=111, msg="hi"):
    scheduler.add_job(bot.SEND_PING_REF, "date", run_date=run_date, id=job_id,
                      kwargs={"channel_id": 222, "user_id": 333, "message": msg,
                              "scope": scope, "target_name": "홍길동"})


def mock_channel(monkeypatch):
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: channel)
    return channel


async def wait_until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


# --- 설정 -------------------------------------------------------------------

def test_scheduler_uses_sqlalchemy_jobstore():
    store = bot.bot.scheduler._jobstores["default"]
    assert isinstance(store, SQLAlchemyJobStore)
    assert store.engine.url.drivername == "sqlite"


def test_db_url_default(monkeypatch):
    monkeypatch.delenv("JOBS_DB_URL", raising=False)
    monkeypatch.delenv("JOBS_DB_PATH", raising=False)
    assert bot.jobs_db_url() == "sqlite:///" + os.path.join(ROOT, "jobs.sqlite")


def test_db_url_from_path_env(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBS_DB_URL", raising=False)
    monkeypatch.setenv("JOBS_DB_PATH", str(tmp_path / "data" / "a.sqlite"))
    assert bot.jobs_db_url() == "sqlite:///" + str(tmp_path / "data" / "a.sqlite")


def test_db_url_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("JOBS_DB_PATH", "/x/y.sqlite")
    monkeypatch.setenv("JOBS_DB_URL", "postgresql://u:p@h/db")
    assert bot.jobs_db_url() == "postgresql://u:p@h/db"


def test_ensure_sqlite_dir_creates_parent(tmp_path):
    target = tmp_path / "a" / "b" / "jobs.sqlite"
    bot.ensure_sqlite_dir("sqlite:///" + str(target))
    assert target.parent.is_dir()
    bot.ensure_sqlite_dir("sqlite:///:memory:")  # 예외 없이 무시
    bot.ensure_sqlite_dir("postgresql://u:p@h/db")


def test_importing_bot_does_not_create_db_file(tmp_path):
    db = tmp_path / "never.sqlite"
    code = "import bot; print(bot.bot.scheduler._jobstores['default'].engine.url)"
    env = {**os.environ, "JOBS_DB_PATH": str(db)}
    env.pop("JOBS_DB_URL", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert str(db) in out.stdout
    assert not db.exists()


# --- 재시작 시나리오 -------------------------------------------------------------

async def test_scheduled_job_survives_restart(restarter):
    restarter.start()
    it = make_interaction()
    await bot.schedule_notification.callback(it, future_text(), make_member(), "회의")
    job_id = bot.bot.scheduler.get_jobs()[0].id
    run_time = bot.bot.scheduler.get_job(job_id).next_run_time

    restarter.start()  # 재시작
    job = bot.bot.scheduler.get_job(job_id)
    assert job is not None
    assert job.func is bot.send_ping and job.func_ref == "bot:send_ping"
    assert job.next_run_time == run_time
    assert job.kwargs == {"channel_id": 222, "user_id": 333, "message": "회의",
                          "scope": 111, "target_name": "홍길동"}
    assert job.misfire_grace_time == bot.MISFIRE_GRACE_SECONDS and job.coalesce is True


async def test_job_arguments_are_plain_types(restarter):
    restarter.start()
    member = make_member()
    await bot.schedule_notification.callback(make_interaction(), future_text(), member, "x")
    job = bot.bot.scheduler.get_jobs()[0]
    assert job.args == ()
    assert all(type(v) in (int, str) for v in job.kwargs.values())


async def test_list_and_cancel_after_restart(restarter):
    s = restarter.start()
    store_job(s, "a001", datetime.now(SEOUL) + timedelta(days=1), msg="저장된 예약")
    store_job(s, "b002", datetime.now(SEOUL) + timedelta(days=2))

    restarter.start()
    it = make_interaction(guild_id=111)
    await bot.list_jobs.callback(it)
    embed = it.response.send_message.call_args.kwargs["embed"]
    assert "저장된 예약" in str([f.value for f in embed.fields])

    await bot.cancel_job.callback(make_interaction(guild_id=111), "a001")
    restarter.start()  # 취소도 DB에 반영되어야 함
    assert bot.bot.scheduler.get_job("a001") is None
    assert bot.bot.scheduler.get_job("b002") is not None


async def test_generate_job_id_checks_persisted_ids(restarter, monkeypatch):
    s = restarter.start()
    store_job(s, "aaaa", datetime.now(SEOUL) + timedelta(days=1))
    s = restarter.start()
    from types import SimpleNamespace
    ids = iter(["aaaa" + "0" * 28, "cccc" + "0" * 28])
    monkeypatch.setattr(bot.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    assert bot.generate_job_id(s) == "cccc"


async def test_missed_within_grace_is_sent_once_after_restart(restarter, monkeypatch):
    # 꺼져 있는 동안(일시정지 상태로 흉내) 예약 시각이 1분 지남 → 허용 시간(5분) 이내라 재시작 직후 1회 전송
    s = restarter.start(paused=True)
    store_job(s, "m001", datetime.now(SEOUL) - timedelta(seconds=60), msg="늦은 알림")
    assert s.get_job("m001") is not None

    channel = mock_channel(monkeypatch)
    restarter.start()
    assert await wait_until(lambda: channel.send.await_count >= 1)
    await asyncio.sleep(0.2)
    assert channel.send.await_count == 1
    assert channel.send.call_args.args[0] == "<@333> 늦은 알림"
    assert bot.bot.scheduler.get_job("m001") is None  # 일회성 예약은 전송 후 삭제


async def test_missed_beyond_grace_is_skipped_and_logged(restarter, monkeypatch, capsys):
    s = restarter.start(paused=True)
    store_job(s, "m002", datetime.now(SEOUL) - timedelta(seconds=bot.MISFIRE_GRACE_SECONDS + 60))

    channel = mock_channel(monkeypatch)
    restarter.start()
    assert await wait_until(lambda: "m002" in capsys.readouterr().out)
    channel.send.assert_not_awaited()
    assert bot.bot.scheduler.get_job("m002") is None


async def test_future_job_fires_after_restart(restarter, monkeypatch):
    s = restarter.start()
    store_job(s, "f001", datetime.now(SEOUL) + timedelta(seconds=1), msg="곧 알림")
    channel = mock_channel(monkeypatch)
    restarter.start()
    assert await wait_until(lambda: channel.send.await_count == 1, timeout=5)
    assert channel.send.call_args.args[0] == "<@333> 곧 알림"


def test_run_as_script_restores_jobs_with_same_module(tmp_path):
    """`python bot.py`(모듈명 __main__)로 실행해도 'bot:send_ping' 이 실행 중인 그 모듈의 함수로 복원되어야 함."""
    db = tmp_path / "jobs.sqlite"
    code = textwrap.dedent(f"""
        import runpy, sys
        sys.path.insert(0, {ROOT!r})  # `python bot.py` 처럼 스크립트 폴더를 import 경로에 추가
        try:
            runpy.run_path({os.path.join(ROOT, 'bot.py')!r}, run_name="__main__")
        except SystemExit:
            pass  # 토큰이 없어 main() 이 종료됨 (봇은 실행되지 않음)
        from apscheduler.util import ref_to_obj
        m = sys.modules["bot"]
        print(m.__name__, ref_to_obj("bot:send_ping") is m.send_ping, "time_parser" in sys.modules)
    """)
    env = {**os.environ, "JOBS_DB_PATH": str(db)}
    env.pop("DISCORD_BOT_TOKEN", None)
    env.pop("JOBS_DB_URL", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["__main__", "True", "True"]


async def test_setup_hook_creates_db_folder_and_restores_jobs(tmp_path, monkeypatch, capsys):
    url = "sqlite:///" + str(tmp_path / "nested" / "dir" / "jobs.sqlite")
    monkeypatch.setattr(bot.bot, "jobs_db_url", url)
    monkeypatch.setattr(bot.bot.tree, "sync", AsyncMock())

    first = bot.create_scheduler(url)
    monkeypatch.setattr(bot.bot, "scheduler", first)
    await bot.bot.setup_hook()  # 폴더가 없어도 DB 파일을 만들고 시작해야 함
    assert (tmp_path / "nested" / "dir" / "jobs.sqlite").exists()
    store_job(first, "s001", datetime.now(SEOUL) + timedelta(days=1))
    first.shutdown(wait=False)

    second = bot.create_scheduler(url)
    monkeypatch.setattr(bot.bot, "scheduler", second)
    try:
        await bot.bot.setup_hook()
        assert second.get_job("s001") is not None
        assert "저장된 예약 1건" in capsys.readouterr().out
    finally:
        second.shutdown(wait=False)


async def test_job_saved_by_script_process_is_restored_by_another_process(tmp_path):
    """프로세스 A(`python bot.py` 방식)가 /예약 으로 저장 → 프로세스 종료 → 새 프로세스(B=이 테스트)가 복원."""
    db_url = "sqlite:///" + str(tmp_path / "jobs.sqlite")
    code = textwrap.dedent(f"""
        import asyncio, runpy, sys
        sys.path.insert(0, {ROOT!r})
        sys.path.insert(0, {os.path.join(ROOT, 'tests')!r})
        try:
            runpy.run_path({os.path.join(ROOT, 'bot.py')!r}, run_name="__main__")
        except SystemExit:
            pass
        m = sys.modules["bot"]
        from test_bot import make_interaction, make_member, future_text

        async def main():
            s = m.create_scheduler({db_url!r})
            s.start()
            m.bot.scheduler = s
            await m.schedule_notification.callback(make_interaction(), future_text(), make_member(), "프로세스 A")
            print(s.get_jobs()[0].id)
            s.shutdown(wait=False)
        asyncio.run(main())
    """)
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_BOT_TOKEN", "JOBS_DB_URL", "JOBS_DB_PATH")}
    out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    job_id = out.stdout.split()[-1]

    s = bot.create_scheduler(db_url)
    s.start(paused=True)
    try:
        job = s.get_job(job_id)
        assert job is not None and job.func is bot.send_ping
        assert job.kwargs["message"] == "프로세스 A"
    finally:
        s.shutdown(wait=False)
