"""polls.py 테스트 (디스코드 연결 없이 mock 으로 검증)."""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import polls  # noqa: E402
from polls import Poll, PollInputError, PollManager, PollStore  # noqa: E402

SEOUL = pytz.timezone("Asia/Seoul")
GUILD = 111
CHANNEL = 222
CREATOR = 1000


# ─────────────────────────────────────────────────────────────────────────────
# 픽스처 / 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

class FakeChannel:
    def __init__(self):
        self.send = AsyncMock()
        self.partials = {}
        self.edit_side_effect = None

    def get_partial_message(self, message_id):
        if message_id not in self.partials:
            pm = MagicMock()
            pm.edit = AsyncMock(side_effect=self.edit_side_effect)
            self.partials[message_id] = pm
        return self.partials[message_id]


class FakeBot:
    def __init__(self, channel=None, cached=True):
        self.channel = channel or FakeChannel()
        self.cached = cached
        self.views = []
        self.fetch_channel = AsyncMock(return_value=self.channel)

    def get_channel(self, channel_id):
        return self.channel if self.cached else None

    def add_view(self, view, message_id=None):
        self.views.append((view, message_id))


def make_user(uid, admin=False, manage_guild=False):
    return SimpleNamespace(
        id=uid,
        guild_permissions=SimpleNamespace(administrator=admin, manage_guild=manage_guild),
    )


_next_msg_id = [900000000000000000]


def make_interaction(user_id=CREATOR, guild_id=GUILD, channel_id=CHANNEL, message_id=None, admin=False,
                     manage_guild=False):
    it = MagicMock()
    it.guild_id = guild_id
    it.channel_id = channel_id
    it.user = make_user(user_id, admin, manage_guild)
    it.response.send_message = AsyncMock()
    it.response.edit_message = AsyncMock()
    it.response.defer = AsyncMock()
    it.followup.send = AsyncMock()
    _next_msg_id[0] += 1
    it.original_response = AsyncMock(return_value=SimpleNamespace(id=_next_msg_id[0]))
    it.message = SimpleNamespace(id=message_id)
    return it


class Clock:
    def __init__(self, t=None):
        self.t = t if t is not None else time.time()

    def __call__(self):
        return self.t


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "polls_test.db")


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
async def manager(bot, db_path):
    m = PollManager(bot, db_path=db_path)
    yield m
    for t in list(m.tasks.values()):
        t.cancel()
    if m._store is not None:
        m.store.close_connection()


async def create(manager, question="점심 메뉴?", options="짜장, 짬뽕, 볶음밥", deadline=None,
                 multi=False, anon=True, user_id=CREATOR, guild_id=GUILD):
    it = make_interaction(user_id=user_id, guild_id=guild_id)
    poll = await manager.handle_create(it, question, options, deadline, multi, anon)
    return poll, it


async def vote(manager, poll, user_id, idx, guild_id=GUILD):
    it = make_interaction(user_id=user_id, guild_id=guild_id, message_id=poll.message_id)
    action = await manager.handle_vote(it, idx)
    return action, it


def sent_text(mock):
    call = mock.await_args
    return call.args[0] if call.args else call.kwargs.get("content")


# ─────────────────────────────────────────────────────────────────────────────
# 입력 검증
# ─────────────────────────────────────────────────────────────────────────────

class TestParseOptions:
    def test_basic(self):
        assert polls.parse_options("짜장, 짬뽕, 볶음밥") == ["짜장", "짬뽕", "볶음밥"]

    def test_strips_and_drops_empty(self):
        assert polls.parse_options(" 짜장 ,, 짬뽕 ,  ,") == ["짜장", "짬뽕"]

    def test_fullwidth_comma(self):
        assert polls.parse_options("짜장，짬뽕") == ["짜장", "짬뽕"]

    @pytest.mark.parametrize("text", ["", "짜장", "짜장,", " , , "])
    def test_too_few(self, text):
        with pytest.raises(PollInputError):
            polls.parse_options(text)

    def test_ten_ok_eleven_rejected(self):
        assert len(polls.parse_options(",".join(f"항목{i}" for i in range(10)))) == 10
        with pytest.raises(PollInputError, match="최대 10개"):
            polls.parse_options(",".join(f"항목{i}" for i in range(11)))

    def test_length_limit_80(self):
        assert polls.parse_options("가" * 80 + ",나") == ["가" * 80, "나"]
        with pytest.raises(PollInputError, match="80자"):
            polls.parse_options("가" * 81 + ",나")

    @pytest.mark.parametrize("text", ["짜장, 짜장", "Pizza, pizza", "a, b, A"])
    def test_duplicates_rejected(self, text):
        with pytest.raises(PollInputError, match="중복"):
            polls.parse_options(text)


class TestValidateQuestion:
    def test_strip(self):
        assert polls.validate_question("  뭐 먹지?  ") == "뭐 먹지?"

    @pytest.mark.parametrize("text", ["", "   "])
    def test_empty(self, text):
        with pytest.raises(PollInputError):
            polls.validate_question(text)

    def test_length(self):
        assert polls.validate_question("가" * 200) == "가" * 200
        with pytest.raises(PollInputError, match="200자"):
            polls.validate_question("가" * 201)


class TestParseDeadline:
    NOW = SEOUL.localize(datetime(2026, 11, 5, 15, 0, 0))

    @pytest.mark.parametrize("text", [None, "", "  "])
    def test_none(self, text):
        assert polls.parse_deadline(text, self.NOW) is None

    def test_relative(self):
        ts = polls.parse_deadline("30분 뒤", self.NOW)
        assert ts == (self.NOW + timedelta(minutes=30)).timestamp()

    def test_clock_time(self):
        ts = polls.parse_deadline("오후 6시", self.NOW)
        assert datetime.fromtimestamp(ts, SEOUL) == SEOUL.localize(datetime(2026, 11, 5, 18, 0))

    def test_invalid_text(self):
        with pytest.raises(PollInputError, match="인식할 수 없습니다"):
            polls.parse_deadline("언젠가", self.NOW)

    def test_past(self):
        with pytest.raises(PollInputError, match="이후"):
            polls.parse_deadline("오늘 오전 9시", self.NOW)

    def test_too_far(self):
        with pytest.raises(PollInputError, match="30일"):
            polls.parse_deadline("2027년 1월 1일 오전 9시", self.NOW)

    def test_within_30_days(self):
        assert polls.parse_deadline("29일 뒤", self.NOW) is not None


# ─────────────────────────────────────────────────────────────────────────────
# 렌더링
# ─────────────────────────────────────────────────────────────────────────────

def make_poll(**kw):
    base = dict(message_id=1, guild_id=GUILD, channel_id=CHANNEL, creator_id=CREATOR,
                question="질문", options=["A", "B", "C"])
    base.update(kw)
    return Poll(**base)


class TestRender:
    @pytest.mark.parametrize("count,total,expected", [
        (0, 0, "░" * 10), (0, 5, "░" * 10), (5, 5, "█" * 10), (1, 2, "█" * 5 + "░" * 5),
        (1, 3, "███" + "░" * 7), (2, 3, "███████" + "░" * 3),
    ])
    def test_bar(self, count, total, expected):
        assert polls.make_bar(count, total) == expected

    def test_percent(self):
        assert polls.percent(0, 0) == 0.0
        assert polls.percent(1, 4) == 25.0

    def test_counts_and_winners(self):
        p = make_poll(votes={1: [0], 2: [1], 3: [1]})
        assert p.counts() == [1, 2, 0]
        assert p.participants == 3
        assert p.winners() == [1]

    def test_winners_tie_and_empty(self):
        assert make_poll(votes={1: [0], 2: [2]}).winners() == [0, 2]
        assert make_poll().winners() == []

    def test_embed_shows_counts_and_percent(self):
        e = polls.build_embed(make_poll(votes={1: [0], 2: [1], 3: [1], 4: [1]}))
        assert "1표 (25.0%)" in e.description
        assert "3표 (75.0%)" in e.description
        assert "0표 (0.0%)" in e.description
        assert "참여 4명" in e.footer.text
        assert "`" + "█" * 8 + "░" * 2 + "`" in e.description

    def test_multi_percent_by_participants(self):
        e = polls.build_embed(make_poll(allow_multiple=True, votes={1: [0, 1], 2: [0]}))
        assert "2표 (100.0%)" in e.description and "1표 (50.0%)" in e.description
        assert "중복 선택 가능" in e.footer.text

    def test_anonymous_has_no_voter_fields(self):
        e = polls.build_embed(make_poll(votes={555: [0]}))
        assert e.fields == [] and "<@555>" not in (e.description or "")

    def test_public_lists_voters(self):
        e = polls.build_embed(make_poll(anonymous=False, votes={555: [0], 666: [0]}))
        assert len(e.fields) == 1 and "<@555>" in e.fields[0].value and "<@666>" in e.fields[0].value
        assert "공개" in e.footer.text

    def test_deadline_timestamp_shown(self):
        e = polls.build_embed(make_poll(closes_at=1_800_000_000.5))
        assert "<t:1800000000:F>" in e.description

    def test_closed_marks_winners(self):
        e = polls.build_embed(make_poll(closed=True, votes={1: [0], 2: [1]}))
        assert e.description.count("🏆") == 2
        assert "마감됨" in e.footer.text

    def test_worst_case_within_discord_limits(self):
        p = make_poll(question="질" * 200, options=[f"{i}" + "가" * 79 for i in range(10)],
                      anonymous=False, allow_multiple=True, closes_at=time.time() + 60,
                      votes={uid: list(range(10)) for uid in range(10 ** 17, 10 ** 17 + 500)})
        e = polls.build_embed(p)
        assert len(e.title) <= 256
        assert len(e.description) <= 4096
        assert len(e) <= 6000
        assert len(e.fields) <= 25
        assert all(len(f.value) <= 1024 and len(f.name) <= 256 for f in e.fields)

    def test_mentions_and_markdown_escaped(self):
        p = make_poll(question="@everyone 모여", options=["@here", "<@&123>"], closed=True, votes={1: [0]})
        e = polls.build_embed(p)
        assert "@everyone" not in e.description
        text = polls.result_text(p)
        assert "@everyone" not in text and "@here" not in text

    def test_result_text_single_winner(self):
        t = polls.result_text(make_poll(votes={1: [0], 2: [0], 3: [1]}))
        assert "🏆 **A** (2표, 66.7%)" in t and "참여 3명" in t
        assert f"/{GUILD}/{CHANNEL}/1" in t

    def test_result_text_tie(self):
        t = polls.result_text(make_poll(votes={1: [0], 2: [2]}))
        assert "동점" in t and "**A**" in t and "**C**" in t and "각 1표" in t

    def test_result_text_no_votes(self):
        assert "참여한 사람이 없어" in polls.result_text(make_poll())


# ─────────────────────────────────────────────────────────────────────────────
# View (persistent)
# ─────────────────────────────────────────────────────────────────────────────

class TestView:
    async def test_template_view_is_persistent_with_fixed_ids(self, manager):
        v = polls.PollView(manager)
        assert v.timeout is None and v.is_persistent()
        assert [c.custom_id for c in v.children] == [f"alram-poll:{i}" for i in range(10)]

    async def test_message_view_labels_and_rows(self, manager):
        p = make_poll(options=[f"항목{i}" for i in range(7)])
        v = polls.make_view(manager, p)
        assert [c.label for c in v.children] == [f"{i + 1}. 항목{i}" for i in range(7)]
        assert [c.row for c in v.children] == [0, 0, 0, 0, 0, 1, 1]
        assert all(not c.disabled for c in v.children)
        assert v.is_persistent()

    async def test_label_truncated_to_80(self, manager):
        v = polls.make_view(manager, make_poll(options=["가" * 80, "나"]))
        assert len(v.children[0].label) == 80

    async def test_disabled_view(self, manager):
        v = polls.make_view(manager, make_poll(), disabled=True)
        assert all(c.disabled for c in v.children)

    async def test_button_callback_routes_to_manager(self, manager):
        manager.handle_vote = AsyncMock()
        v = polls.make_view(manager, make_poll())
        it = make_interaction(message_id=1)
        await v.children[2].callback(it)
        manager.handle_vote.assert_awaited_once_with(it, 2)


# ─────────────────────────────────────────────────────────────────────────────
# 생성
# ─────────────────────────────────────────────────────────────────────────────

class TestCreate:
    async def test_success(self, manager):
        poll, it = await create(manager)
        assert poll is not None
        kwargs = it.response.send_message.await_args.kwargs
        assert kwargs["allowed_mentions"].everyone is False
        assert kwargs["allowed_mentions"].roles is False
        assert kwargs["allowed_mentions"].users is False
        assert len(kwargs["view"].children) == 3
        assert kwargs["embed"].title == "📊 점심 메뉴?"
        stored = manager.store.get_poll(poll.message_id)
        assert stored.options == ["짜장", "짬뽕", "볶음밥"]
        assert stored.creator_id == CREATOR and stored.guild_id == GUILD and stored.channel_id == CHANNEL
        assert stored.allow_multiple is False and stored.anonymous is True and stored.closes_at is None
        assert poll.message_id not in manager.tasks

    async def test_flags_stored(self, manager):
        poll, _ = await create(manager, multi=True, anon=False)
        stored = manager.store.get_poll(poll.message_id)
        assert stored.allow_multiple is True and stored.anonymous is False

    async def test_deadline_schedules_task(self, manager):
        poll, _ = await create(manager, deadline="30분 뒤")
        stored = manager.store.get_poll(poll.message_id)
        assert stored.closes_at == pytest.approx(time.time() + 1800, abs=5)
        assert poll.message_id in manager.tasks

    async def test_dm_rejected(self, manager):
        poll, it = await create(manager, guild_id=None)
        assert poll is None
        assert it.response.send_message.await_args.kwargs["ephemeral"] is True

    @pytest.mark.parametrize("question,options,deadline,msg", [
        ("q", "하나", None, "2개 이상"),
        ("q", ",".join(map(str, range(11))), None, "최대 10개"),
        ("q", "가" * 81 + ",b", None, "80자"),
        ("가" * 201, "a,b", None, "200자"),
        ("q", "a,b", "아무때나", "마감 시간"),
        ("q", "a,a", None, "중복"),
    ])
    async def test_invalid_input_is_ephemeral(self, manager, question, options, deadline, msg):
        poll, it = await create(manager, question=question, options=options, deadline=deadline)
        assert poll is None
        call = it.response.send_message.await_args
        assert msg in call.args[0] and call.kwargs["ephemeral"] is True
        assert manager.store.open_polls() == []

    async def test_open_poll_limit_per_guild(self, manager, monkeypatch):
        monkeypatch.setattr(polls, "MAX_OPEN_POLLS_PER_GUILD", 2)
        assert (await create(manager))[0] is not None
        assert (await create(manager))[0] is not None
        poll, it = await create(manager)
        assert poll is None and "너무 많습니다" in it.response.send_message.await_args.args[0]
        # 다른 서버는 영향 없음
        assert (await create(manager, guild_id=999))[0] is not None


# ─────────────────────────────────────────────────────────────────────────────
# 투표 / 변경 / 취소
# ─────────────────────────────────────────────────────────────────────────────

class TestVote:
    async def test_vote_updates_embed_and_replies_ephemeral(self, manager):
        poll, _ = await create(manager)
        action, it = await vote(manager, poll, 1, 0)
        assert action == "added"
        embed = it.response.edit_message.await_args.kwargs["embed"]
        assert "1표 (100.0%)" in embed.description and "참여 1명" in embed.footer.text
        f = it.followup.send.await_args
        assert "짜장" in f.args[0] and f.kwargs["ephemeral"] is True
        assert manager.store.get_poll(poll.message_id).votes == {1: [0]}

    async def test_change_vote_single(self, manager):
        poll, _ = await create(manager)
        await vote(manager, poll, 1, 0)
        action, it = await vote(manager, poll, 1, 1)
        assert action == "changed" and "변경" in it.followup.send.await_args.args[0]
        assert manager.store.get_poll(poll.message_id).votes == {1: [1]}

    async def test_cancel_by_reclick(self, manager):
        poll, _ = await create(manager)
        await vote(manager, poll, 1, 0)
        action, it = await vote(manager, poll, 1, 0)
        assert action == "removed" and "취소" in it.followup.send.await_args.args[0]
        p = manager.store.get_poll(poll.message_id)
        assert p.votes == {} and p.participants == 0
        assert "참여 0명" in it.response.edit_message.await_args.kwargs["embed"].footer.text

    async def test_revote_after_cancel(self, manager):
        poll, _ = await create(manager)
        await vote(manager, poll, 1, 0)
        await vote(manager, poll, 1, 0)
        action, _ = await vote(manager, poll, 1, 2)
        assert action == "added"
        assert manager.store.get_poll(poll.message_id).votes == {1: [2]}

    async def test_multiple_allowed(self, manager):
        poll, _ = await create(manager, multi=True)
        assert (await vote(manager, poll, 1, 0))[0] == "added"
        assert (await vote(manager, poll, 1, 2))[0] == "added"
        p = manager.store.get_poll(poll.message_id)
        assert sorted(p.votes[1]) == [0, 2] and p.counts() == [1, 0, 1] and p.participants == 1
        assert (await vote(manager, poll, 1, 0))[0] == "removed"
        assert manager.store.get_poll(poll.message_id).votes == {1: [2]}

    async def test_single_never_holds_two(self, manager):
        poll, _ = await create(manager)
        for idx in [0, 1, 2, 1, 0]:
            await vote(manager, poll, 7, idx)
        assert manager.store.get_poll(poll.message_id).votes == {7: [0]}

    async def test_many_users(self, manager):
        poll, _ = await create(manager)
        for uid, idx in [(1, 0), (2, 0), (3, 1), (4, 2), (5, 0)]:
            await vote(manager, poll, uid, idx)
        p = manager.store.get_poll(poll.message_id)
        assert p.counts() == [3, 1, 1] and p.participants == 5

    async def test_concurrent_votes(self, manager):
        poll, _ = await create(manager)
        await asyncio.gather(*(vote(manager, poll, uid, uid % 3) for uid in range(30)))
        assert manager.store.get_poll(poll.message_id).counts() == [10, 10, 10]

    async def test_unknown_poll(self, manager):
        it = make_interaction(message_id=424242)
        assert await manager.handle_vote(it, 0) is None
        assert "찾을 수 없습니다" in it.response.send_message.await_args.args[0]
        assert it.response.send_message.await_args.kwargs["ephemeral"] is True

    async def test_other_guild_cannot_vote(self, manager):
        poll, _ = await create(manager)
        action, it = await vote(manager, poll, 1, 0, guild_id=999)
        assert action is None and manager.store.get_poll(poll.message_id).votes == {}

    async def test_out_of_range_index(self, manager):
        poll, _ = await create(manager)  # 3개 항목
        action, it = await vote(manager, poll, 1, 5)
        assert action is None and "잘못된 항목" in it.response.send_message.await_args.args[0]

    async def test_closed_poll_rejects(self, manager):
        poll, _ = await create(manager)
        await manager.close_poll(poll.message_id)
        action, it = await vote(manager, poll, 1, 0)
        assert action is None and "마감된" in it.response.send_message.await_args.args[0]
        assert manager.store.get_poll(poll.message_id).votes == {}

    async def test_expired_but_not_yet_closed_triggers_close(self, manager, bot):
        poll, _ = await create(manager, deadline="30분 뒤")
        manager.tasks.pop(poll.message_id).cancel()
        manager.clock = Clock(time.time() + 3600)
        action, it = await vote(manager, poll, 1, 0)
        assert action is None and "마감된" in it.response.send_message.await_args.args[0]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert manager.store.get_poll(poll.message_id).closed is True
        bot.channel.send.assert_awaited_once()

    async def test_reply_does_not_ping(self, manager):
        poll, _ = await create(manager, options="@everyone, <@&1>")
        _, it = await vote(manager, poll, 1, 0)
        f = it.followup.send.await_args
        assert f.kwargs["allowed_mentions"].everyone is False and "@everyone" not in f.args[0]


# ─────────────────────────────────────────────────────────────────────────────
# 마감
# ─────────────────────────────────────────────────────────────────────────────

class TestClose:
    async def test_close_disables_and_announces(self, manager, bot):
        poll, _ = await create(manager)
        await vote(manager, poll, 1, 0)
        await vote(manager, poll, 2, 0)
        await vote(manager, poll, 3, 1)
        assert await manager.close_poll(poll.message_id) is True
        edit = bot.channel.partials[poll.message_id].edit.await_args.kwargs
        assert all(c.disabled for c in edit["view"].children)
        assert len(edit["view"].children) == 3
        assert "마감됨" in edit["embed"].footer.text
        send = bot.channel.send.await_args
        assert "**짜장** (2표, 66.7%)" in send.args[0]
        assert send.kwargs["allowed_mentions"].everyone is False
        assert manager.store.get_poll(poll.message_id).closed is True

    async def test_tie_announced(self, manager, bot):
        poll, _ = await create(manager)
        await vote(manager, poll, 1, 0)
        await vote(manager, poll, 2, 2)
        await manager.close_poll(poll.message_id)
        text = bot.channel.send.await_args.args[0]
        assert "동점" in text and "**짜장**" in text and "**볶음밥**" in text and "**짬뽕**" not in text

    async def test_three_way_tie(self, manager, bot):
        poll, _ = await create(manager)
        for uid in range(3):
            await vote(manager, poll, uid, uid)
        await manager.close_poll(poll.message_id)
        assert "각 1표" in bot.channel.send.await_args.args[0]

    async def test_no_votes(self, manager, bot):
        poll, _ = await create(manager)
        await manager.close_poll(poll.message_id)
        assert "참여한 사람이 없어" in bot.channel.send.await_args.args[0]

    async def test_close_is_idempotent(self, manager, bot):
        poll, _ = await create(manager)
        assert await manager.close_poll(poll.message_id) is True
        assert await manager.close_poll(poll.message_id) is False
        assert bot.channel.send.await_count == 1

    async def test_concurrent_close_announces_once(self, manager, bot):
        poll, _ = await create(manager)
        results = await asyncio.gather(*(manager.close_poll(poll.message_id) for _ in range(5)))
        assert results.count(True) == 1 and bot.channel.send.await_count == 1

    async def test_deleted_message_still_announces(self, bot, db_path):
        bot.channel.edit_side_effect = discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")
        m = PollManager(bot, db_path=db_path)
        poll, _ = await create(m)
        assert await m.close_poll(poll.message_id) is True
        bot.channel.send.assert_awaited_once()
        assert m.store.get_poll(poll.message_id).closed is True

    async def test_fetch_channel_when_not_cached(self, db_path):
        b = FakeBot(cached=False)
        m = PollManager(b, db_path=db_path)
        poll, _ = await create(m)
        await m.close_poll(poll.message_id)
        b.fetch_channel.assert_awaited_once_with(CHANNEL)
        b.channel.send.assert_awaited_once()

    async def test_channel_unavailable(self, db_path):
        b = FakeBot(cached=False)
        b.fetch_channel.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no")
        m = PollManager(b, db_path=db_path)
        poll, _ = await create(m)
        assert await m.close_poll(poll.message_id) is True
        assert m.store.get_poll(poll.message_id).closed is True

    async def test_auto_close_at_deadline(self, manager, bot):
        poll, _ = await create(manager)
        p = manager.store.get_poll(poll.message_id)
        p.closes_at = time.time() + 0.05
        task = manager.schedule_close(p)
        await asyncio.wait_for(task, 2)
        assert manager.store.get_poll(poll.message_id).closed is True
        bot.channel.send.assert_awaited_once()
        assert poll.message_id not in manager.tasks

    async def test_auto_close_not_before_deadline(self, manager, bot):
        poll, _ = await create(manager, deadline="30분 뒤")
        await asyncio.sleep(0.05)
        assert manager.store.get_poll(poll.message_id).closed is False
        bot.channel.send.assert_not_awaited()

    async def test_reschedule_replaces_old_task(self, manager):
        poll, _ = await create(manager, deadline="30분 뒤")
        old = manager.tasks[poll.message_id]
        manager.schedule_close(manager.store.get_poll(poll.message_id))
        await asyncio.sleep(0)
        assert old.cancelled() and manager.tasks[poll.message_id] is not old


class TestManualClose:
    async def _poll(self, manager, deadline=None):
        poll, _ = await create(manager, deadline=deadline)
        return poll

    async def test_creator_can_close(self, manager, bot):
        poll = await self._poll(manager)
        it = make_interaction(user_id=CREATOR)
        assert await manager.handle_manual_close(it, str(poll.message_id)) is True
        assert "마감했습니다" in it.followup.send.await_args.args[0]
        bot.channel.send.assert_awaited_once()

    @pytest.mark.parametrize("admin,manage", [(True, False), (False, True)])
    async def test_admin_can_close(self, manager, admin, manage):
        poll = await self._poll(manager)
        it = make_interaction(user_id=5, admin=admin, manage_guild=manage)
        assert await manager.handle_manual_close(it, f"  {poll.message_id} ") is True

    async def test_others_cannot_close(self, manager, bot):
        poll = await self._poll(manager)
        it = make_interaction(user_id=5)
        assert await manager.handle_manual_close(it, str(poll.message_id)) is False
        assert "만든 사람이나 관리자" in it.response.send_message.await_args.args[0]
        assert manager.store.get_poll(poll.message_id).closed is False

    @pytest.mark.parametrize("text", ["", "abc", "12a", "-5", "1.5"])
    async def test_invalid_id(self, manager, text):
        it = make_interaction()
        assert await manager.handle_manual_close(it, text) is False
        assert "숫자" in it.response.send_message.await_args.args[0]

    async def test_not_found(self, manager):
        it = make_interaction()
        assert await manager.handle_manual_close(it, "123456") is False
        assert "찾을 수 없습니다" in it.response.send_message.await_args.args[0]

    async def test_other_guild_looks_not_found(self, manager):
        poll = await self._poll(manager)
        it = make_interaction(guild_id=999, admin=True)
        assert await manager.handle_manual_close(it, str(poll.message_id)) is False
        assert "찾을 수 없습니다" in it.response.send_message.await_args.args[0]

    async def test_already_closed(self, manager):
        poll = await self._poll(manager)
        await manager.close_poll(poll.message_id)
        it = make_interaction()
        assert await manager.handle_manual_close(it, str(poll.message_id)) is False
        assert "이미 마감" in it.response.send_message.await_args.args[0]

    async def test_manual_close_cancels_timer(self, manager, bot):
        poll = await self._poll(manager, deadline="30분 뒤")
        task = manager.tasks[poll.message_id]
        await manager.handle_manual_close(make_interaction(), str(poll.message_id))
        await asyncio.sleep(0)
        assert task.cancelled() and poll.message_id not in manager.tasks
        assert bot.channel.send.await_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 저장소 / 재시작 복원
# ─────────────────────────────────────────────────────────────────────────────

class TestRestore:
    async def test_votes_persist_across_restart(self, bot, db_path):
        m1 = PollManager(bot, db_path=db_path)
        poll, _ = await create(m1, multi=True, anon=False)
        await vote(m1, poll, 1, 0)
        await vote(m1, poll, 1, 1)
        await vote(m1, poll, 2, 2)
        m1.store.close_connection()

        bot2 = FakeBot()
        m2 = PollManager(bot2, db_path=db_path)
        assert await m2.restore() == 1
        p = m2.store.get_poll(poll.message_id)
        assert p.votes == {1: [0, 1], 2: [2]} and p.allow_multiple and not p.anonymous
        # 재시작 후에도 같은 메시지에서 계속 투표 가능
        action, _ = await vote(m2, poll, 2, 2)
        assert action == "removed"
        m2.store.close_connection()

    async def test_restore_registers_persistent_view_once(self, bot, db_path):
        m = PollManager(bot, db_path=db_path)
        await m.restore()
        await m.restore()
        assert len(bot.views) == 1
        view, message_id = bot.views[0]
        assert message_id is None and view.timeout is None and view.is_persistent()
        assert {c.custom_id for c in view.children} == {f"alram-poll:{i}" for i in range(10)}
        m.store.close_connection()

    async def test_restore_reschedules_open_polls_with_deadline(self, bot, db_path):
        m1 = PollManager(bot, db_path=db_path)
        with_deadline, _ = await create(m1, deadline="1시간 뒤")
        without, _ = await create(m1)
        closed, _ = await create(m1, deadline="2시간 뒤")
        await m1.close_poll(closed.message_id)
        for t in m1.tasks.values():
            t.cancel()
        m1.store.close_connection()

        m2 = PollManager(FakeBot(), db_path=db_path)
        assert await m2.restore() == 2
        assert set(m2.tasks) == {with_deadline.message_id}
        for t in m2.tasks.values():
            t.cancel()
        m2.store.close_connection()

    async def test_restore_closes_expired_polls(self, bot, db_path):
        m1 = PollManager(bot, db_path=db_path)
        poll, _ = await create(m1, deadline="10분 뒤")
        m1.tasks.pop(poll.message_id).cancel()
        m1.store.close_connection()

        bot2 = FakeBot()
        m2 = PollManager(bot2, db_path=db_path, clock=Clock(time.time() + 3600))
        await m2.restore()
        await asyncio.wait_for(m2.tasks[poll.message_id], 2)
        assert m2.store.get_poll(poll.message_id).closed is True
        bot2.channel.send.assert_awaited_once()
        m2.store.close_connection()

    async def test_restore_vote_through_template_view(self, bot, db_path):
        """재시작 후 등록된 템플릿 view 의 버튼으로 기존 메시지 투표가 처리되는지."""
        m1 = PollManager(bot, db_path=db_path)
        poll, _ = await create(m1)
        m1.store.close_connection()

        bot2 = FakeBot()
        m2 = PollManager(bot2, db_path=db_path)
        await m2.restore()
        template = bot2.views[0][0]
        it = make_interaction(user_id=9, message_id=poll.message_id)
        await template.children[1].callback(it)
        assert m2.store.get_poll(poll.message_id).votes == {9: [1]}
        m2.store.close_connection()

    async def test_module_restore_uses_registered_manager(self, db_path):
        b = FakeBot()
        b.poll_manager = PollManager(b, db_path=db_path)
        assert await polls.restore(b) == 0
        assert len(b.views) == 1
        b.poll_manager.store.close_connection()

    def test_db_path_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("POLLS_DB_PATH", str(tmp_path / "x.db"))
        assert PollManager(FakeBot()).db_path == str(tmp_path / "x.db")
        monkeypatch.delenv("POLLS_DB_PATH")
        assert PollManager(FakeBot()).db_path == polls.DEFAULT_DB_PATH
        assert os.path.basename(polls.DEFAULT_DB_PATH) == "polls.db" and os.path.isabs(polls.DEFAULT_DB_PATH)

    def test_store_lazy_open(self, tmp_path):
        path = tmp_path / "lazy.db"
        m = PollManager(FakeBot(), db_path=str(path))
        assert not path.exists()
        _ = m.store
        assert path.exists()
        m.store.close_connection()

    def test_store_roundtrip_unicode(self, db_path):
        s = PollStore(db_path)
        s.add_poll(make_poll(message_id=5, question="🍜 뭐 먹지?", options=["짜장 🍝", "짬뽕"], closes_at=123.5))
        p = s.get_poll(5)
        assert p.question == "🍜 뭐 먹지?" and p.options == ["짜장 🍝", "짬뽕"] and p.closes_at == 123.5
        assert s.mark_closed(5) is True and s.mark_closed(5) is False
        assert s.open_polls() == []
        s.close_connection()

    def test_count_open_in_guild(self, db_path):
        s = PollStore(db_path)
        s.add_poll(make_poll(message_id=1))
        s.add_poll(make_poll(message_id=2))
        s.add_poll(make_poll(message_id=3, guild_id=5))
        s.mark_closed(2)
        assert s.count_open_in_guild(GUILD) == 1 and s.count_open_in_guild(5) == 1
        s.close_connection()


# ─────────────────────────────────────────────────────────────────────────────
# bot.py 연동
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_commands_registered_on_bot(self):
        import bot as bot_module
        names = {c.name for c in bot_module.bot.tree.get_commands()}
        assert {"투표", "투표마감", "예약", "예약목록", "예약취소"} <= names
        assert isinstance(bot_module.bot.poll_manager, PollManager)
        cmd = bot_module.bot.tree.get_command("투표")
        assert cmd.guild_only is True
        assert bot_module.bot.tree.get_command("투표마감").guild_only is True

    def test_command_parameters(self):
        import bot as bot_module
        cmd = bot_module.bot.tree.get_command("투표")
        params = {p.name: p for p in cmd.parameters}
        assert list(params) == ["질문", "항목", "마감", "중복허용", "익명"]
        assert params["질문"].required and params["항목"].required
        assert not params["마감"].required
        assert params["중복허용"].default is False and params["익명"].default is True

    def test_import_does_not_create_db(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        polls.register(FakeTreeBot())
        assert not (tmp_path / "polls.db").exists()

    def test_bot_py_keeps_crlf(self):
        data = open(os.path.join(ROOT, "bot.py"), "rb").read()
        assert data.count(b"\r\n") == data.count(b"\n")
        assert b"import polls" in data and b"polls.register(bot)" in data and b"await polls.restore(self)" in data


class FakeTreeBot:
    def __init__(self):
        self.tree = discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))


def test_command_payloads_valid_for_sync():
    """tree.sync 로 보낼 페이로드: 서버 전용(contexts=[0]), 설명 100자 이하."""
    import bot as bot_module
    for name in ["투표", "투표마감"]:
        d = bot_module.bot.tree.get_command(name).to_dict(bot_module.bot.tree)
        assert d["contexts"] == [0]
        assert len(d["description"]) <= 100
        assert all(len(o["description"]) <= 100 and len(o["name"]) <= 32 for o in d["options"])
