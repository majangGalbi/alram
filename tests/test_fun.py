"""fun.py 테스트 (디스코드 연결 없이 mock 으로 검증)."""
import os
import random
import sys
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fun  # noqa: E402


def make_interaction(guild=True, user=None):
    it = MagicMock()
    it.guild = MagicMock() if guild else None
    it.guild_id = 1 if guild else None
    it.user = user or SimpleNamespace(id=1, voice=None)
    it.response.send_message = AsyncMock()
    it.response.is_done = MagicMock(return_value=False)
    it.followup.send = AsyncMock()
    return it


def sent(it):
    args, kwargs = it.response.send_message.call_args
    return (args[0] if args else kwargs.get("content")), kwargs


def member(uid, bot=False):
    return SimpleNamespace(id=uid, bot=bot, mention=f"<@{uid}>", display_name=f"m{uid}")


def voice_user(members, channel_name="일반"):
    channel = SimpleNamespace(name=channel_name, members=members)
    return SimpleNamespace(id=members[0].id if members else 1, voice=SimpleNamespace(channel=channel))


def assert_no_mass_ping(kw):
    am = kw["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users is False


# ───────────── 주사위 파싱 ─────────────

class TestParseDice:
    @pytest.mark.parametrize("expr,expected", [
        ("1d6", (1, 6, 0)),
        ("2d6+3", (2, 6, 3)),
        ("3d8-1", (3, 8, -1)),
        ("d20", (1, 20, 0)),
        ("D20", (1, 20, 0)),
        ("2D10", (2, 10, 0)),
        (" 2d6 + 3 ", (2, 6, 3)),
        ("2 d 6", (2, 6, 0)),
        ("100d1000", (100, 1000, 0)),
        ("1d2", (1, 2, 0)),
        ("1d6+100000", (1, 6, 100000)),
        ("1d6-100000", (1, 6, -100000)),
        ("1d6+0", (1, 6, 0)),
        ("01d06", (1, 6, 0)),
    ])
    def test_valid(self, expr, expected):
        s = fun.parse_dice(expr)
        assert (s.count, s.sides, s.modifier) == expected

    @pytest.mark.parametrize("expr", [None, "", "   "])
    def test_default(self, expr):
        assert fun.parse_dice(expr) == fun.DiceSpec(1, 6, 0)

    @pytest.mark.parametrize("expr", [
        "abc", "2d", "d", "6", "2x6", "2d6+", "2d6++3", "2d6*2", "-2d6", "2d-6",
        "1d6+1d4", "2.5d6", "２d6", "1d٦", "@everyone", "1d6 3", "d6d6",
    ])
    def test_invalid_format(self, expr):
        with pytest.raises(fun.InputError):
            fun.parse_dice(expr)

    @pytest.mark.parametrize("expr", ["0d6", "101d6", "999999d6"])
    def test_count_limit(self, expr):
        with pytest.raises(fun.InputError, match="개수"):
            fun.parse_dice(expr)

    @pytest.mark.parametrize("expr", ["1d0", "1d1", "1d1001", "1d99999999"])
    def test_sides_limit(self, expr):
        with pytest.raises(fun.InputError, match="면"):
            fun.parse_dice(expr)

    def test_modifier_limit(self):
        with pytest.raises(fun.InputError, match="보정"):
            fun.parse_dice("1d6+100001")

    def test_length_limit(self):
        with pytest.raises(fun.InputError, match="너무 깁니다"):
            fun.parse_dice("1d6+" + "0" * 40)

    def test_str(self):
        assert str(fun.DiceSpec(2, 6, 3)) == "2d6+3"
        assert str(fun.DiceSpec(1, 20, -2)) == "1d20-2"
        assert str(fun.DiceSpec(1, 6)) == "1d6"


class TestRollDice:
    def test_range_and_count(self):
        spec = fun.DiceSpec(100, 6)
        rolls = fun.roll_dice(spec)
        assert len(rolls) == 100 and all(1 <= r <= 6 for r in rolls)

    def test_all_faces_appear(self):
        rolls = fun.roll_dice(fun.DiceSpec(100, 4)) + fun.roll_dice(fun.DiceSpec(100, 4))
        assert set(rolls) == {1, 2, 3, 4}

    def test_uses_given_rng(self):
        a = fun.roll_dice(fun.DiceSpec(10, 20), random.Random(42))
        b = fun.roll_dice(fun.DiceSpec(10, 20), random.Random(42))
        assert a == b

    def test_default_rng_is_system_random(self):
        assert isinstance(fun.RNG, random.SystemRandom)

    def test_roughly_uniform(self):
        c = Counter(fun.roll_dice(fun.DiceSpec(100, 6)) + [x for _ in range(59) for x in fun.roll_dice(fun.DiceSpec(100, 6))])
        # 6000회, 기대값 1000. 매우 느슨한 범위(약 ±8σ)로 확인
        assert all(750 < c[f] < 1250 for f in range(1, 7))


# ───────────── 뽑기 ─────────────

class TestParseItems:
    def test_basic(self):
        assert fun.parse_items("a, b, c") == ["a", "b", "c"]

    def test_strip_and_empty(self):
        assert fun.parse_items(" a ,, b ,  ,c,") == ["a", "b", "c"]

    def test_fullwidth_and_newline(self):
        assert fun.parse_items("짜장，짬뽕、볶음밥\n탕수육") == ["짜장", "짬뽕", "볶음밥", "탕수육"]

    def test_dedupe_keeps_order(self):
        assert fun.parse_items("b, a, b, c, a") == ["b", "a", "c"]

    def test_single(self):
        assert fun.parse_items("혼자") == ["혼자"]

    @pytest.mark.parametrize("text", [None, "", " , , "])
    def test_empty_rejected(self, text):
        with pytest.raises(fun.InputError):
            fun.parse_items(text)

    def test_input_too_long(self):
        with pytest.raises(fun.InputError, match="너무 깁니다"):
            fun.parse_items("a" * (fun.PICK_INPUT_MAX_LEN + 1))

    def test_item_too_long(self):
        with pytest.raises(fun.InputError, match="각 항목"):
            fun.parse_items("ok, " + "x" * (fun.PICK_ITEM_MAX_LEN + 1))

    def test_too_many_items(self):
        with pytest.raises(fun.InputError, match="최대"):
            fun.parse_items(",".join(str(i) for i in range(fun.PICK_MAX_ITEMS + 1)))

    def test_max_items_ok(self):
        assert len(fun.parse_items(",".join(str(i) for i in range(fun.PICK_MAX_ITEMS)))) == fun.PICK_MAX_ITEMS


class TestPick:
    def test_unique_results(self):
        items = list("abcdefghij")
        for _ in range(50):
            r = fun.pick(items, 5)
            assert len(r) == 5 and len(set(r)) == 5 and set(r) <= set(items)

    def test_pick_all(self):
        assert sorted(fun.pick(["a", "b", "c"], 3)) == ["a", "b", "c"]

    @pytest.mark.parametrize("count", [0, -1, 4])
    def test_bad_count(self, count):
        with pytest.raises(fun.InputError):
            fun.pick(["a", "b", "c"], count)

    def test_fair(self):
        c = Counter(fun.pick(["a", "b", "c"], 1)[0] for _ in range(3000))
        assert all(800 < c[k] < 1200 for k in "abc")


# ───────────── 팀 분배 ─────────────

class TestSplitTeams:
    @pytest.mark.parametrize("n,k", [(2, 2), (5, 2), (10, 3), (7, 4), (11, 5), (25, 10), (9, 9)])
    def test_sizes_balanced(self, n, k):
        teams = fun.split_teams(list(range(n)), k)
        sizes = [len(t) for t in teams]
        assert len(teams) == k
        assert max(sizes) - min(sizes) <= 1
        assert sum(sizes) == n

    def test_everyone_exactly_once(self):
        members = list(range(13))
        teams = fun.split_teams(members, 4)
        flat = [m for t in teams for m in t]
        assert sorted(flat) == members

    def test_input_not_mutated(self):
        members = list(range(10))
        fun.split_teams(members, 3)
        assert members == list(range(10))

    def test_too_few_members(self):
        with pytest.raises(fun.InputError, match="적습니다"):
            fun.split_teams([1], 2)

    def test_zero_teams(self):
        with pytest.raises(fun.InputError):
            fun.split_teams([1, 2], 0)

    def test_randomized(self):
        results = {tuple(map(tuple, fun.split_teams(list(range(8)), 2))) for _ in range(30)}
        assert len(results) > 1

    def test_each_member_spread_evenly_across_teams(self):
        # 4명을 2팀으로 2000번: 각 멤버가 1팀에 들어갈 확률 ≈ 1/2
        counts = Counter()
        for _ in range(2000):
            t1 = fun.split_teams(list(range(4)), 2)[0]
            counts.update(t1)
        assert all(800 < counts[m] < 1200 for m in range(4))

    def test_deterministic_with_seeded_rng(self):
        a = fun.split_teams(list(range(10)), 3, random.Random(1))
        b = fun.split_teams(list(range(10)), 3, random.Random(1))
        assert a == b


class TestVoiceMembers:
    def test_not_in_voice(self):
        assert fun.voice_members(SimpleNamespace(voice=None)) is None

    def test_no_voice_attr(self):
        assert fun.voice_members(SimpleNamespace()) is None

    def test_voice_without_channel(self):
        assert fun.voice_members(SimpleNamespace(voice=SimpleNamespace(channel=None))) is None

    def test_excludes_bots(self):
        ms = [member(1), member(2, bot=True), member(3)]
        assert [m.id for m in fun.voice_members(voice_user(ms))] == [1, 3]


# ───────────── 명령 핸들러 ─────────────

class TestHandleDice:
    async def test_default(self):
        it = make_interaction()
        await fun.handle_dice(it, None)
        text, kw = sent(it)
        assert text.startswith("🎲 **1d6** → **")
        total = int(text.split("→ **")[1].split("**")[0])
        assert 1 <= total <= 6
        assert_no_mass_ping(kw)

    async def test_with_modifier_shows_detail(self):
        rng = MagicMock()
        rng.randint = MagicMock(side_effect=[2, 5])
        it = make_interaction()
        await fun.handle_dice(it, "2d6+3", rng)
        text, _ = sent(it)
        assert "**10**" in text and "[2, 5] +3" in text

    async def test_negative_modifier(self):
        rng = MagicMock()
        rng.randint = MagicMock(return_value=1)
        it = make_interaction()
        await fun.handle_dice(it, "1d4-3", rng)
        text, _ = sent(it)
        assert "**-2**" in text and "[1] -3" in text

    async def test_invalid_is_ephemeral(self):
        it = make_interaction()
        await fun.handle_dice(it, "@everyone")
        text, kw = sent(it)
        assert text.startswith("❌") and kw["ephemeral"] is True
        assert_no_mass_ping(kw)

    async def test_limit_is_ephemeral(self):
        it = make_interaction()
        await fun.handle_dice(it, "1000d6")
        assert sent(it)[1]["ephemeral"] is True

    async def test_max_dice_message_fits(self):
        it = make_interaction()
        await fun.handle_dice(it, "100d1000+100000")
        text, _ = sent(it)
        assert len(text) <= 2000

    async def test_error_followup_when_done(self):
        it = make_interaction()
        it.response.is_done = MagicMock(return_value=True)
        await fun.handle_dice(it, "nope")
        it.followup.send.assert_awaited_once()
        assert it.followup.send.call_args.kwargs["ephemeral"] is True


class TestHandlePick:
    async def test_pick_one(self):
        it = make_interaction()
        await fun.handle_pick(it, "짜장, 짬뽕, 볶음밥")
        text, kw = sent(it)
        assert "3개 중 1개" in text
        assert any(f"**{x}**" in text for x in ("짜장", "짬뽕", "볶음밥"))
        assert_no_mass_ping(kw)
        assert not kw.get("ephemeral")

    async def test_pick_many(self):
        it = make_interaction()
        await fun.handle_pick(it, "a, b, c, d", 3)
        assert "4개 중 3개" in sent(it)[0]

    async def test_mentions_in_items_not_pinged(self):
        it = make_interaction()
        await fun.handle_pick(it, "@everyone, @here, <@&123>", 3)
        text, kw = sent(it)
        assert "@everyone" in text
        assert_no_mass_ping(kw)

    async def test_markdown_escaped(self):
        it = make_interaction()
        await fun.handle_pick(it, "**x**")
        assert "\\*\\*x\\*\\*" in sent(it)[0]

    async def test_count_too_big_ephemeral(self):
        it = make_interaction()
        await fun.handle_pick(it, "a, b", 3)
        text, kw = sent(it)
        assert "1~2" in text and kw["ephemeral"] is True

    async def test_too_long_ephemeral(self):
        it = make_interaction()
        await fun.handle_pick(it, "a" * 2000)
        assert sent(it)[1]["ephemeral"] is True

    async def test_long_result_clipped(self):
        it = make_interaction()
        items = ",".join(f"{i:03d}" + "x" * 90 for i in range(30))
        await fun.handle_pick(it, items, 30)
        assert len(sent(it)[0]) <= 1900


class TestHandleTeams:
    async def test_not_in_voice(self):
        it = make_interaction(user=SimpleNamespace(id=1, voice=None))
        await fun.handle_teams(it, 2)
        text, kw = sent(it)
        assert "음성 채널" in text and kw["ephemeral"] is True

    async def test_dm(self):
        it = make_interaction(guild=False)
        await fun.handle_teams(it, 2)
        assert sent(it)[1]["ephemeral"] is True

    @pytest.mark.parametrize("k", [0, 1, 11])
    async def test_bad_team_count(self, k):
        it = make_interaction(user=voice_user([member(i) for i in range(1, 6)]))
        await fun.handle_teams(it, k)
        assert sent(it)[1]["ephemeral"] is True

    async def test_too_few_humans(self):
        ms = [member(1), member(2, bot=True), member(3, bot=True)]
        it = make_interaction(user=voice_user(ms))
        await fun.handle_teams(it, 2)
        text, kw = sent(it)
        assert "적습니다" in text and kw["ephemeral"] is True

    async def test_split_embed(self):
        ms = [member(i) for i in range(1, 8)] + [member(99, bot=True)]
        it = make_interaction(user=voice_user(ms, "게임방"))
        await fun.handle_teams(it, 3)
        _, kw = sent(it)
        e = kw["embed"]
        assert "게임방" in e.title and "7명" in e.title and "3팀" in e.title
        assert len(e.fields) == 3
        sizes = sorted(int(f.name.split("(")[1].split("명")[0]) for f in e.fields)
        assert sizes == [2, 2, 3]
        allm = "\n".join(f.value for f in e.fields)
        assert "<@99>" not in allm
        assert all(f"<@{i}>" in allm for i in range(1, 8))
        assert_no_mass_ping(kw)

    async def test_default_two_teams(self):
        it = make_interaction(user=voice_user([member(i) for i in range(1, 5)]))
        await fun.handle_teams(it)
        assert len(sent(it)[1]["embed"].fields) == 2


class TestRegister:
    def test_commands_registered(self):
        from discord import app_commands
        fake = SimpleNamespace(tree=MagicMock(), intents=SimpleNamespace(voice_states=True))
        fun.register(fake)
        cmds = [c.args[0] for c in fake.tree.add_command.call_args_list]
        assert [c.name for c in cmds] == ["주사위", "뽑기", "팀나누기"]
        assert all(isinstance(c, app_commands.Command) for c in cmds)

    def test_input_length_limits_declared(self):
        fake = SimpleNamespace(tree=MagicMock())
        fun.register(fake)
        dice, pick_cmd, teams = [c.args[0] for c in fake.tree.add_command.call_args_list]
        # 문자열 Range 는 min_value/max_value 로 저장되고 디스코드에는 min_length/max_length 로 전송됨
        assert dice.parameters[0].max_value == fun.DICE_EXPR_MAX_LEN
        assert dice.to_dict(MagicMock())["options"][0]["max_length"] == fun.DICE_EXPR_MAX_LEN
        p = {x.name: x for x in pick_cmd.parameters}
        assert p["항목"].max_value == fun.PICK_INPUT_MAX_LEN and p["항목"].required
        assert p["개수"].max_value == fun.PICK_MAX_ITEMS
        assert (teams.parameters[0].min_value, teams.parameters[0].max_value) == (fun.TEAM_MIN, fun.TEAM_MAX)

    def test_warns_without_voice_intent(self, capsys):
        fake = SimpleNamespace(tree=MagicMock(), intents=SimpleNamespace(voice_states=False))
        fun.register(fake)
        assert "voice_states" in capsys.readouterr().out

    def test_bot_has_voice_states_intent(self):
        import bot
        assert bot.bot.intents.voice_states is True
        names = {c.name for c in bot.bot.tree.get_commands()}
        assert {"주사위", "뽑기", "팀나누기"} <= names

    async def test_callback_invokes_handler(self):
        fake = SimpleNamespace(tree=MagicMock())
        fun.register(fake)
        dice = fake.tree.add_command.call_args_list[0].args[0]
        it = make_interaction()
        await dice.callback(it, "2d2")
        assert sent(it)[0].startswith("🎲 **2d2**")
