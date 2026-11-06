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


def make_interaction(guild_id=111, channel_id=222, user_id=444):
    it = MagicMock()
    it.guild_id = guild_id
    it.user.id = user_id
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
    scheduler.add_job(lambda: None, "date", run_date=datetime.now(SEOUL) + timedelta(days=1), id="aaaa0000")
    ids = iter(["aaaa0000" + "0" * 24, "bbbb0000" + "0" * 24])
    monkeypatch.setattr(bot.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    assert bot.generate_job_id(scheduler) == "bbbb0000"


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


# --- P5: /예약 오류 메시지는 본인에게만 보여야 함 ------------------------------------
# 공개 defer(ephemeral=False) 후 첫 followup 은 ephemeral=True 여도 공개로 표시되므로,
# 오류는 defer 없이 첫 응답(response.send_message, ephemeral=True)으로 보내야 함.

@pytest.mark.parametrize("when, msg", [
    ("아무말", "회의"),                         # 해석 불가
    ("2020년 1월 1일 오전 9시", "회의"),          # 과거
    (None, "가" * (bot.MAX_MESSAGE_LENGTH + 1)),  # 너무 긴 메시지
])
async def test_schedule_errors_are_ephemeral_first_response(scheduler, when, msg):
    it = make_interaction()
    it.response.is_done = MagicMock(return_value=False)
    await bot.schedule_notification.callback(it, when or future_text(), make_member(), msg)
    it.response.defer.assert_not_awaited()
    it.followup.send.assert_not_awaited()
    it.response.send_message.assert_awaited_once()
    assert it.response.send_message.call_args.kwargs.get("ephemeral") is True
    assert it.response.send_message.call_args.args[0].startswith("❌")
    assert scheduler.get_jobs() == []


async def test_schedule_success_is_public(scheduler):
    calls = []
    it = make_interaction()
    it.response.defer = AsyncMock(side_effect=lambda **kw: calls.append(("defer", kw)))
    it.followup.send = AsyncMock(side_effect=lambda *a, **kw: calls.append(("followup", kw)))
    await bot.schedule_notification.callback(it, future_text(), make_member(), "회의")
    assert calls[0] == ("defer", {"ephemeral": False})
    assert calls[1][0] == "followup" and not calls[1][1].get("ephemeral")
    it.response.send_message.assert_not_awaited()
    assert scheduler.get_jobs()[0].func is bot.send_ping


async def test_error_handler_is_ephemeral_before_response():
    it = make_interaction()
    it.response.is_done = MagicMock(return_value=False)
    await bot.on_app_command_error(it, bot.app_commands.AppCommandError("x"))
    assert it.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_other_commands_do_not_defer(scheduler):
    """/예약목록, /예약취소 는 defer 없이 첫 응답에서 ephemeral 여부를 정하므로 P5 문제가 없음."""
    list_it, cancel_it = make_interaction(), make_interaction()
    await bot.list_jobs.callback(list_it)
    await bot.cancel_job.callback(cancel_it, "zzzz")
    for it in (list_it, cancel_it):
        it.response.defer.assert_not_awaited()
        it.followup.send.assert_not_awaited()
        assert it.response.send_message.call_args.kwargs["ephemeral"] is True
