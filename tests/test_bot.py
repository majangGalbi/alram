"""bot.py 회귀 테스트 (디스코드 연결 없이 mock 으로 검증)."""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402  (__main__ 가드 덕분에 import 시 봇/웹서버가 실행되지 않아야 함)

SEOUL = pytz.timezone("Asia/Seoul")


def make_interaction(guild_id=111, channel_id=222):
    it = MagicMock()
    it.guild_id = guild_id
    it.channel_id = channel_id
    it.response.defer = AsyncMock()
    it.response.send_message = AsyncMock()
    it.response.is_done = MagicMock(return_value=True)
    it.followup.send = AsyncMock()
    return it


def make_member(uid=333, name="홍길동"):
    return SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=name)


@pytest.fixture
async def scheduler(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    s = AsyncIOScheduler(timezone="Asia/Seoul",
                         job_defaults={"misfire_grace_time": bot.MISFIRE_GRACE_SECONDS, "coalesce": True})
    s.start()
    monkeypatch.setattr(bot.bot, "scheduler", s)
    yield s
    s.shutdown(wait=False)


def future_text():
    t = datetime.now(SEOUL) + timedelta(days=30)
    hour = t.hour % 12 or 12
    ampm = "오전" if t.hour < 12 else "오후"
    return f"{t.year}년 {t.month}월 {t.day}일 {ampm} {hour}시 0분 0초"


def test_import_has_no_side_effects():
    assert callable(bot.main)


def test_bot_blocks_everyone_and_role_mentions():
    am = bot.bot.allowed_mentions
    assert am.everyone is False and am.roles is False


async def test_default_misfire_grace_is_not_one_second():
    assert bot.bot.scheduler._job_defaults["misfire_grace_time"] == bot.MISFIRE_GRACE_SECONDS > 1


def test_main_requires_token(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        bot.main()


def test_format_korean_time():
    assert bot.format_korean_time(datetime(2026, 5, 23, 13, 30, 0)) == "2026년 05월 23일 오후 01시 30분 00초"
    assert bot.format_korean_time(datetime(2026, 5, 23, 0, 5, 0)) == "2026년 05월 23일 오전 12시 05분 00초"


async def test_generate_job_id_avoids_existing(scheduler, monkeypatch):
    scheduler.add_job(lambda: None, "date", run_date=datetime.now(SEOUL) + timedelta(days=1), id="aaaa")
    ids = iter(["aaaa" + "0" * 28, "bbbb" + "0" * 28])
    monkeypatch.setattr(bot.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    assert bot.generate_job_id(scheduler) == "bbbb"


async def test_schedule_creates_scoped_job(scheduler):
    it = make_interaction()
    await bot.schedule_notification.callback(it, future_text(), make_member(), "회의 @everyone")
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    kw = jobs[0].kwargs
    assert kw["scope"] == 111 and kw["user_id"] == 333 and kw["channel_id"] == 222
    assert jobs[0].misfire_grace_time == bot.MISFIRE_GRACE_SECONDS
    assert "예약 완료" in it.followup.send.call_args.args[0]


async def test_schedule_rejects_too_long_message(scheduler):
    it = make_interaction()
    await bot.schedule_notification.callback(it, future_text(), make_member(), "가" * (bot.MAX_MESSAGE_LENGTH + 1))
    assert scheduler.get_jobs() == []


async def _add(scheduler, job_id, scope, msg="hi"):
    scheduler.add_job(bot.send_ping, "date", run_date=datetime.now(SEOUL) + timedelta(days=1), id=job_id,
                      kwargs={"channel_id": 1, "user_id": 2, "message": msg, "scope": scope, "target_name": "x"})


async def test_list_only_shows_current_server(scheduler):
    await _add(scheduler, "a001", 111, "우리 서버")
    await _add(scheduler, "b002", 999, "다른 서버 비밀")
    it = make_interaction(guild_id=111)
    await bot.list_jobs.callback(it)
    embed = it.response.send_message.call_args.kwargs["embed"]
    text = str([f.value for f in embed.fields])
    assert "우리 서버" in text and "다른 서버 비밀" not in text


async def test_list_respects_embed_limits(scheduler):
    for i in range(30):
        await _add(scheduler, f"j{i:03d}", 111, "x" * 2000)
    it = make_interaction(guild_id=111)
    await bot.list_jobs.callback(it)
    embed = it.response.send_message.call_args.kwargs["embed"]
    assert len(embed.fields) == 25
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert "5건" in embed.footer.text


async def test_cancel_other_server_job_is_refused(scheduler):
    await _add(scheduler, "c003", 999)
    it = make_interaction(guild_id=111)
    await bot.cancel_job.callback(it, "c003")
    assert scheduler.get_job("c003") is not None


async def test_cancel_own_job_normalizes_input(scheduler):
    await _add(scheduler, "d004", 111)
    it = make_interaction(guild_id=111)
    await bot.cancel_job.callback(it, "  D004 ")
    assert scheduler.get_job("d004") is None


async def test_send_ping_fetches_uncached_channel_and_restricts_mentions(monkeypatch):
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: None)
    monkeypatch.setattr(bot.bot, "fetch_channel", AsyncMock(return_value=channel))
    await bot.send_ping(1, 42, "@everyone 회의")
    args, kwargs = channel.send.call_args
    assert args[0].startswith("<@42>")
    am = kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and [u.id for u in am.users] == [42]


async def test_send_ping_swallows_and_logs_errors(monkeypatch, capsys):
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: None)
    monkeypatch.setattr(bot.bot, "fetch_channel", AsyncMock(side_effect=RuntimeError("boom")))
    await bot.send_ping(1, 42, "x")
    assert "알림 전송 실패" in capsys.readouterr().out
