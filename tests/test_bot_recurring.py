"""bot.py 반복 예약 테스트 (디스코드 연결 없이 mock 으로 검증)."""
import os
import pickle
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

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


async def schedule(text, it=None, member=None, msg="알림"):
    it = it or make_interaction()
    await bot.schedule_notification.callback(it, text, member or make_member(), msg)
    return it


def _last_call(it):
    # 성공은 공개 defer 뒤 followup, 오류는 defer 없이 첫 응답(ephemeral)으로 보냄 (REVIEW.md P5)
    return it.followup.send.call_args if it.followup.send.called else it.response.send_message.call_args


def last_text(it):
    return _last_call(it).args[0]


def last_ephemeral(it):
    return _last_call(it).kwargs.get("ephemeral", False)


def cron_fields(trigger):
    return {f.name: str(f) for f in trigger.fields}


# ─────────────────────────────────────────────────────────────────────────────
# 등록
# ─────────────────────────────────────────────────────────────────────────────

CRON_CASES = [
    ("매일 오전 6시", {"hour": "6", "minute": "0", "second": "0", "day_of_week": "*", "day": "*"}),
    ("매일 아침 7시 30분", {"hour": "7", "minute": "30"}),
    ("평일 8시", {"day_of_week": "mon,tue,wed,thu,fri", "hour": "8"}),
    ("주말 오전 10시", {"day_of_week": "sat,sun", "hour": "10"}),
    ("매주 월요일 9시", {"day_of_week": "mon", "hour": "9"}),
    ("매주 월수금 오후 6시", {"day_of_week": "mon,wed,fri", "hour": "18"}),
    ("매주 월~금 9시", {"day_of_week": "mon,tue,wed,thu,fri", "hour": "9"}),
    ("매월 1일 오전 9시", {"day": "1", "hour": "9"}),
    ("매월 말일 오후 6시", {"day": "last", "hour": "18"}),
    ("매년 12월 25일 오전 9시", {"month": "12", "day": "25", "hour": "9"}),
    ("매시간", {"minute": "0", "hour": "*"}),
    ("매시 30분", {"minute": "30"}),
    ("every day 7am", {"hour": "7"}),
    ("every mon, wed, fri 9am", {"day_of_week": "mon,wed,fri", "hour": "9"}),
    ("weekdays 9:30am", {"day_of_week": "mon,tue,wed,thu,fri", "hour": "9", "minute": "30"}),
]


@pytest.mark.parametrize("text,fields", CRON_CASES)
async def test_schedule_recurring_cron(scheduler, text, fields):
    it = await schedule(text)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1, last_text(it)
    job = jobs[0]
    assert isinstance(job.trigger, CronTrigger)
    assert str(job.trigger.timezone) == "Asia/Seoul"
    got = cron_fields(job.trigger)
    for k, v in fields.items():
        assert got[k] == v, (k, got)
    assert job.kwargs["recurrence"]
    assert "반복 알림 예약 완료" in last_text(it)


INTERVAL_CASES = [("10분마다", 600), ("30분마다", 1800), ("2시간마다", 7200), ("1시간 30분마다", 5400),
                  ("every 2 hours", 7200)]


@pytest.mark.parametrize("text,seconds", INTERVAL_CASES)
async def test_schedule_recurring_interval(scheduler, text, seconds):
    await schedule(text)
    job = scheduler.get_jobs()[0]
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(seconds=seconds)
    assert str(job.trigger.timezone) == "Asia/Seoul"
    delta = (job.next_run_time - datetime.now(SEOUL)).total_seconds()
    assert seconds - 15 <= delta <= seconds


async def test_recurring_job_kwargs_are_scoped_and_primitive(scheduler):
    await schedule("매일 오전 7시", make_interaction(guild_id=111, channel_id=222), make_member(333, "길동"), "기상!")
    job = scheduler.get_jobs()[0]
    assert job.kwargs == {
        "channel_id": 222, "user_id": 333, "message": "기상!", "scope": 111,
        "target_name": "길동", "recurrence": "매일 오전 7시", "owner_id": 444,
    }
    assert all(type(v) in (int, str) for v in job.kwargs.values())
    assert job.func is bot.send_ping
    assert job.func_ref == "bot:send_ping"   # 모듈 수준 함수 → 텍스트 참조로 복원 가능
    assert job.misfire_grace_time == bot.MISFIRE_GRACE_SECONDS
    assert job.coalesce is True


@pytest.mark.parametrize("text", ["매일 오전 7시", "매주 월수금 9시", "30분마다", "매월 말일 오후 6시", "매시간"])
async def test_recurring_job_state_is_picklable(scheduler, text):
    """SQLAlchemyJobStore 는 job.__getstate__() 를 pickle 해 저장한다."""
    await schedule(text)
    job = scheduler.get_jobs()[0]
    state = pickle.loads(pickle.dumps(job.__getstate__()))
    assert state["func"] == "bot:send_ping"
    assert state["kwargs"] == job.kwargs
    assert type(state["trigger"]) is type(job.trigger)


async def test_dm_scope_uses_channel(scheduler):
    await schedule("매일 9시", make_interaction(guild_id=None, channel_id=555))
    assert scheduler.get_jobs()[0].kwargs["scope"] == 555


async def test_confirmation_shows_rule_and_next_run(scheduler):
    it = await schedule("매주 월수금 오후 6시", msg="운동")
    job = scheduler.get_jobs()[0]
    text = last_text(it)
    assert f"**예약 번호(ID):** {job.id}" in text
    assert "🔁 **반복:** 매주 월·수·금 오후 6시" in text
    assert bot.format_korean_time(job.next_run_time) in text
    assert "<@333>" in text and "운동" in text
    assert f"/예약취소 {job.id}" in text
    assert last_ephemeral(it) is False


async def test_next_run_is_in_future_and_matches_rule(scheduler):
    await schedule("매주 월요일 9시")
    nxt = scheduler.get_jobs()[0].next_run_time
    assert nxt > datetime.now(SEOUL)
    local = nxt.astimezone(SEOUL)
    assert local.weekday() == 0 and (local.hour, local.minute, local.second) == (9, 0, 0)


async def test_cron_job_keeps_firing(scheduler):
    await schedule("평일 8시")
    job = scheduler.get_jobs()[0]
    t = job.next_run_time
    seen = []
    for _ in range(10):
        seen.append(t.astimezone(SEOUL))
        t = job.trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    assert all(d.weekday() < 5 and d.hour == 8 for d in seen)
    assert len({d.date() for d in seen}) == 10


# ─────────────────────────────────────────────────────────────────────────────
# 거부
# ─────────────────────────────────────────────────────────────────────────────

BAD = [
    ("매일", "몇 시"),
    ("매주 9시", "요일"),
    ("5분마다", "최소 10분"),
    ("every 5 minutes", "최소 10분"),
    ("2일마다 9시", "일/주 단위"),
    ("매일 3시 회의", "이해할 수 없는"),
    ("30분마다 9시", "함께 쓸 수 없습니다"),
    ("매월 32일 9시", "1~31"),
    ("매년 2월 30일 9시", "존재하지 않는"),
]


@pytest.mark.parametrize("text,msg", BAD)
async def test_bad_recurring_input_is_rejected(scheduler, text, msg):
    it = await schedule(text)
    assert scheduler.get_jobs() == []
    assert "반복 시간을 인식할 수 없습니다" in last_text(it) and msg in last_text(it)
    assert last_ephemeral(it) is True


async def test_recurring_rejects_too_long_message(scheduler):
    it = await schedule("매일 9시", msg="가" * (bot.MAX_MESSAGE_LENGTH + 1))
    assert scheduler.get_jobs() == []
    assert f"{bot.MAX_MESSAGE_LENGTH}자" in last_text(it)


async def test_recurring_limit_per_scope(scheduler):
    for _ in range(bot.MAX_RECURRING_JOBS_PER_SCOPE):
        await schedule("매일 9시", make_interaction(guild_id=111))
    it = await schedule("매일 10시", make_interaction(guild_id=111))
    assert "최대" in last_text(it) and last_ephemeral(it) is True
    assert bot.count_recurring_jobs(scheduler, 111) == bot.MAX_RECURRING_JOBS_PER_SCOPE
    # 다른 서버는 영향 없음
    await schedule("매일 10시", make_interaction(guild_id=999))
    assert bot.count_recurring_jobs(scheduler, 999) == 1


async def test_one_shot_jobs_do_not_count_toward_recurring_limit(scheduler):
    scheduler.add_job(bot.send_ping, "date", run_date=datetime.now(SEOUL) + timedelta(days=1), id="zz01",
                      kwargs={"channel_id": 1, "user_id": 2, "message": "x", "scope": 111, "target_name": "x"})
    assert bot.count_recurring_jobs(scheduler, 111) == 0


async def test_unfireable_rule_is_rejected(scheduler, monkeypatch):
    trig = MagicMock()
    trig.get_next_fire_time.return_value = None
    monkeypatch.setattr(bot, "build_trigger", lambda rec: trig)
    it = await schedule("매일 9시")
    assert scheduler.get_jobs() == []
    assert "실행될 시각이 없는" in last_text(it)


# ─────────────────────────────────────────────────────────────────────────────
# 1회성 예약은 기존대로
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["30분 뒤", "내일 오후 3시", "다음주 월요일 9시", "in 10 minutes"])
async def test_one_shot_still_uses_date_trigger(scheduler, text):
    it = await schedule(text)
    job = scheduler.get_jobs()[0]
    assert isinstance(job.trigger, DateTrigger)
    assert "recurrence" not in job.kwargs
    assert "✅ 알림 예약 완료" in last_text(it)


async def test_one_shot_error_message_mentions_recurring_examples(scheduler):
    it = await schedule("내일 3시 회의")
    assert "반복" in last_text(it) and "매일 오전 7시" in last_text(it)
    assert scheduler.get_jobs() == []


def test_time_option_description_within_discord_limit():
    cmd = bot.bot.tree.get_command("예약")
    desc = next(p.description for p in cmd.parameters if p.name == "일시")
    assert len(desc) <= 100 and "매일" in desc


# ─────────────────────────────────────────────────────────────────────────────
# ID 충돌 방지
# ─────────────────────────────────────────────────────────────────────────────

async def test_recurring_job_id_avoids_existing(scheduler, monkeypatch):
    await schedule("매일 9시")
    first = scheduler.get_jobs()[0].id
    ids = iter([first + "0" * 24, "beef0000" + "0" * 24])
    monkeypatch.setattr(bot.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    await schedule("매일 10시")
    assert {j.id for j in scheduler.get_jobs()} == {first, "beef0000"}


# ─────────────────────────────────────────────────────────────────────────────
# /예약목록
# ─────────────────────────────────────────────────────────────────────────────

async def test_list_shows_recurrence_and_next_run(scheduler):
    await schedule("매주 월수금 오후 6시", make_interaction(guild_id=111), msg="운동")
    await schedule("30분 뒤", make_interaction(guild_id=111), msg="한번만")
    it = make_interaction(guild_id=111)
    await bot.list_jobs.callback(it)
    embed = it.response.send_message.call_args.kwargs["embed"]
    values = [f.value for f in embed.fields]
    rec = next(v for v in values if "운동" in v)
    one = next(v for v in values if "한번만" in v)
    job = next(j for j in scheduler.get_jobs() if j.kwargs["message"] == "운동")
    assert "🔁 **반복:** 매주 월·수·금 오후 6시" in rec
    assert f"⏭️ **다음 알림:** {bot.format_korean_time(job.next_run_time)}" in rec
    assert "🔁" not in one and "📅 **일시:**" in one


async def test_list_hides_other_server_recurring(scheduler):
    await schedule("매일 9시", make_interaction(guild_id=999), msg="다른 서버 비밀")
    await schedule("매일 9시", make_interaction(guild_id=111), msg="우리 서버")
    it = make_interaction(guild_id=111)
    await bot.list_jobs.callback(it)
    text = str([f.value for f in it.response.send_message.call_args.kwargs["embed"].fields])
    assert "우리 서버" in text and "다른 서버 비밀" not in text


async def test_list_paused_recurring(scheduler):
    await schedule("매일 9시", make_interaction(guild_id=111))
    scheduler.get_jobs()[0].pause()
    it = make_interaction(guild_id=111)
    await bot.list_jobs.callback(it)
    value = it.response.send_message.call_args.kwargs["embed"].fields[0].value
    assert "일시정지됨" in value and "🔁" in value


async def test_list_recurring_respects_field_limit(scheduler):
    for _ in range(3):
        await schedule("매일 9시", make_interaction(guild_id=111), msg="x" * 1500)
    it = make_interaction(guild_id=111)
    await bot.list_jobs.callback(it)
    assert all(len(f.value) <= 1024 for f in it.response.send_message.call_args.kwargs["embed"].fields)


# ─────────────────────────────────────────────────────────────────────────────
# /예약취소
# ─────────────────────────────────────────────────────────────────────────────

async def test_cancel_recurring_job(scheduler):
    await schedule("매일 9시", make_interaction(guild_id=111))
    job_id = scheduler.get_jobs()[0].id
    it = make_interaction(guild_id=111)
    await bot.cancel_job.callback(it, f" {job_id.upper()} ")
    assert scheduler.get_job(job_id) is None
    assert "반복 알림" in it.response.send_message.call_args.args[0]


async def test_cancel_other_server_recurring_is_refused(scheduler):
    await schedule("매일 9시", make_interaction(guild_id=999))
    job_id = scheduler.get_jobs()[0].id
    it = make_interaction(guild_id=111)
    await bot.cancel_job.callback(it, job_id)
    assert scheduler.get_job(job_id) is not None
    assert it.response.send_message.call_args.kwargs.get("ephemeral") is True


async def test_cancel_one_shot_message_unchanged(scheduler):
    await schedule("30분 뒤", make_interaction(guild_id=111))
    job_id = scheduler.get_jobs()[0].id
    it = make_interaction(guild_id=111)
    await bot.cancel_job.callback(it, job_id)
    msg = it.response.send_message.call_args.args[0]
    assert "알림이 성공적으로 취소" in msg and "반복" not in msg


# ─────────────────────────────────────────────────────────────────────────────
# 실행 (send_ping)
# ─────────────────────────────────────────────────────────────────────────────

async def test_recurring_job_run_sends_safe_mention(scheduler, monkeypatch):
    await schedule("매일 9시", member=make_member(42), msg="@everyone @here <@&1> 기상")
    job = scheduler.get_jobs()[0]
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: channel)
    await job.func(*job.args, **job.kwargs)
    args, kwargs = channel.send.call_args
    assert args[0].startswith("<@42> ")
    am = kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and [u.id for u in am.users] == [42]
    assert scheduler.get_job(job.id) is not None  # 반복 예약은 실행 후에도 남아 있음


async def test_send_ping_accepts_recurrence_kwarg(monkeypatch, capsys):
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: None)
    monkeypatch.setattr(bot.bot, "fetch_channel", AsyncMock(side_effect=RuntimeError("boom")))
    await bot.send_ping(1, 42, "x", scope=1, target_name="a", recurrence="매일 오전 9시")
    assert "알림 전송 실패" in capsys.readouterr().out


def test_build_trigger_types():
    from time_parser import parse_recurrence
    assert isinstance(bot.build_trigger(parse_recurrence("매일 9시")), CronTrigger)
    assert isinstance(bot.build_trigger(parse_recurrence("30분마다")), IntervalTrigger)
    assert str(bot.build_trigger(parse_recurrence("30분마다")).timezone) == "Asia/Seoul"


@pytest.mark.parametrize("text", ["매일 몇 시", "5분마다", "매월 32일 9시"])
async def test_recurring_errors_are_private_and_not_deferred(scheduler, text):
    """반복 예약 오류도 공개 defer 없이 본인에게만(ephemeral) 보여야 함 (P5)."""
    it = await schedule(text)
    it.response.defer.assert_not_awaited()
    it.followup.send.assert_not_awaited()
    assert it.response.send_message.call_args.kwargs.get("ephemeral") is True
    assert scheduler.get_jobs() == []


async def test_recurring_success_defers_publicly_and_stores_ref(scheduler):
    it = await schedule("매일 오전 7시")
    it.response.defer.assert_awaited_once_with(ephemeral=False)
    assert not last_ephemeral(it)
    (job,) = scheduler.get_jobs()
    assert job.func_ref == bot.SEND_PING_REF
