"""leveling.py 테스트 (디스코드 연결 없이 mock + 임시 SQLite 로 검증)."""
import os
import random
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import leveling as lv  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return lv.LevelStore(str(tmp_path / "lv.db"))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRole:
    def __init__(self, rid, position, managed=False, default=False):
        self.id = rid
        self.position = position
        self.managed = managed
        self._default = default
        self.mention = f"<@&{rid}>"

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position


def make_me(top_position=10, manage_roles=True):
    perms = SimpleNamespace(manage_roles=manage_roles, administrator=False, send_messages=True, view_channel=True)
    return SimpleNamespace(top_role=FakeRole(999, top_position), guild_permissions=perms)


def make_guild(gid=1, roles=(), me=None):
    role_map = {r.id: r for r in roles}
    g = MagicMock()
    g.id = gid
    g.me = me or make_me()
    g.get_role = MagicMock(side_effect=role_map.get)
    g.get_channel = MagicMock(return_value=None)
    return g


def make_channel(can_send=True):
    ch = MagicMock()
    ch.send = AsyncMock()
    ch.permissions_for = MagicMock(return_value=SimpleNamespace(send_messages=can_send, view_channel=True))
    ch.mention = "<#5>"
    ch.id = 5
    return ch


def make_member(uid=100, bot=False, roles=()):
    m = MagicMock()
    m.id = uid
    m.bot = bot
    m.mention = f"<@{uid}>"
    m.display_name = f"user{uid}"
    m.roles = list(roles)
    m.add_roles = AsyncMock()
    return m


def make_message(guild, author, channel=None, webhook_id=None):
    return SimpleNamespace(guild=guild, author=author, channel=channel or make_channel(), webhook_id=webhook_id)


def make_manager(store, clock=None, seed=0):
    return lv.LevelManager(store, rng=random.Random(seed), clock=clock or Clock())


def make_interaction(guild_id=1, user=None, admin=True, guild=None):
    it = MagicMock()
    it.guild_id = guild_id
    it.guild = guild or make_guild(guild_id or 1)
    perms = SimpleNamespace(administrator=False, manage_guild=admin)
    it.user = user or SimpleNamespace(id=100, bot=False, display_name="철수", guild_permissions=perms)
    if user is None:
        it.user.guild_permissions = perms
    it.response.send_message = AsyncMock()
    it.response.is_done = MagicMock(return_value=False)
    it.followup.send = AsyncMock()
    return it


# ───────────── 레벨 공식 ─────────────

class TestFormula:
    def test_xp_to_next(self):
        assert lv.xp_to_next(0) == 100
        assert lv.xp_to_next(1) == 155
        assert lv.xp_to_next(2) == 220
        assert lv.xp_to_next(10) == 1100

    def test_total_matches_sum(self):
        for level in range(0, 60):
            assert lv.total_xp_for_level(level) == sum(lv.xp_to_next(i) for i in range(level))

    @pytest.mark.parametrize("xp,level", [(0, 0), (99, 0), (100, 1), (254, 1), (255, 2), (474, 2), (475, 3)])
    def test_boundaries(self, xp, level):
        assert lv.level_from_xp(xp) == level

    def test_boundaries_many_levels(self):
        for level in range(1, 100):
            t = lv.total_xp_for_level(level)
            assert lv.level_from_xp(t) == level
            assert lv.level_from_xp(t - 1) == level - 1

    def test_progress(self):
        assert lv.level_progress(130) == (1, 30, 155)
        assert lv.level_progress(0) == (0, 0, 100)

    def test_negative(self):
        assert lv.level_from_xp(-5) == 0
        with pytest.raises(ValueError):
            lv.xp_to_next(-1)

    def test_progress_bar(self):
        assert lv.progress_bar(0, 100, 10) == "▱" * 10
        assert lv.progress_bar(50, 100, 10) == "▰" * 5 + "▱" * 5
        assert lv.progress_bar(200, 100, 10) == "▰" * 10


# ───────────── 쿨다운 / XP 지급 ─────────────

class TestCooldown:
    def test_first_message_counts(self, store):
        r = store.add_xp(1, 100, 20, now=1000.0)
        assert r.xp == 20 and r.gained == 20

    def test_within_cooldown_ignored(self, store):
        store.add_xp(1, 100, 20, now=1000.0)
        assert store.add_xp(1, 100, 20, now=1059.9) is None
        assert store.get_xp(1, 100) == 20

    def test_after_cooldown_counts(self, store):
        store.add_xp(1, 100, 20, now=1000.0)
        assert store.add_xp(1, 100, 20, now=1060.0).xp == 40

    def test_cooldown_per_user(self, store):
        store.add_xp(1, 100, 20, now=1000.0)
        assert store.add_xp(1, 200, 20, now=1001.0) is not None

    def test_cooldown_per_guild(self, store):
        store.add_xp(1, 100, 20, now=1000.0)
        assert store.add_xp(2, 100, 20, now=1001.0) is not None

    def test_level_up_detected(self, store):
        store.add_xp(1, 100, 90, now=0)
        r = store.add_xp(1, 100, 15, now=100)
        assert (r.old_level, r.new_level, r.leveled_up) == (0, 1, True)

    async def test_manager_random_range_and_cooldown(self, store):
        clock = Clock()
        mgr = make_manager(store, clock)
        guild = make_guild()
        author = make_member()
        gains = []
        for _ in range(30):
            r = await mgr.on_message(make_message(guild, author))
            gains.append(r.gained)
            assert await mgr.on_message(make_message(guild, author)) is None  # 같은 시각 → 쿨다운
            clock.t += 60
        assert all(lv.XP_MIN <= g <= lv.XP_MAX for g in gains)
        assert store.get_xp(1, 100) == sum(gains)

    async def test_bot_dm_webhook_ignored(self, store):
        mgr = make_manager(store)
        assert await mgr.on_message(make_message(make_guild(), make_member(bot=True))) is None
        assert await mgr.on_message(make_message(None, make_member())) is None
        assert await mgr.on_message(make_message(make_guild(), make_member(), webhook_id=123)) is None
        assert store.top(1) == []


# ───────────── 레벨업 알림 설정 ─────────────

async def _level_up(store, guild, author, channel):
    store.add_xp(guild.id, author.id, 99, now=0)
    mgr = make_manager(store, Clock(1000))
    return mgr, await mgr.on_message(make_message(guild, author, channel))


class TestAnnounce:
    async def test_default_on_same_channel_mentions_only_user(self, store):
        guild, author, ch = make_guild(), make_member(), make_channel()
        _, r = await _level_up(store, guild, author, ch)
        assert r.leveled_up
        ch.send.assert_awaited_once()
        args, kwargs = ch.send.call_args
        assert "레벨 1" in args[0] and author.mention in args[0]
        am = kwargs["allowed_mentions"]
        assert am.everyone is False and am.roles is False and am.users == [author]

    async def test_no_announce_without_level_up(self, store):
        mgr = make_manager(store)
        ch = make_channel()
        await mgr.on_message(make_message(make_guild(), make_member(), ch))
        ch.send.assert_not_awaited()

    async def test_off(self, store):
        store.set_settings(1, False, None)
        guild, author, ch = make_guild(), make_member(), make_channel()
        await _level_up(store, guild, author, ch)
        ch.send.assert_not_awaited()

    async def test_custom_channel(self, store):
        target = make_channel()
        store.set_settings(1, True, 5)
        guild, author, ch = make_guild(), make_member(), make_channel()
        guild.get_channel = MagicMock(return_value=target)
        await _level_up(store, guild, author, ch)
        ch.send.assert_not_awaited()
        target.send.assert_awaited_once()

    async def test_missing_custom_channel_falls_back(self, store):
        store.set_settings(1, True, 5)
        guild, author, ch = make_guild(), make_member(), make_channel()
        await _level_up(store, guild, author, ch)
        ch.send.assert_awaited_once()

    async def test_no_send_permission(self, store):
        guild, author, ch = make_guild(), make_member(), make_channel(can_send=False)
        await _level_up(store, guild, author, ch)
        ch.send.assert_not_awaited()

    async def test_send_failure_does_not_raise(self, store):
        guild, author, ch = make_guild(), make_member(), make_channel()
        ch.send.side_effect = discord.HTTPException(MagicMock(status=500), "x")
        _, r = await _level_up(store, guild, author, ch)
        assert r.leveled_up

    async def test_settings_command(self, store):
        mgr = make_manager(store)
        it = make_interaction()
        ch = make_channel()
        await mgr.handle_settings(it, False, ch)
        s = store.get_settings(1)
        assert s.announce is False and s.channel_id == 5
        assert it.response.send_message.call_args.kwargs["ephemeral"] is True

    async def test_settings_requires_admin(self, store):
        mgr = make_manager(store)
        it = make_interaction(admin=False)
        await mgr.handle_settings(it, False, None)
        assert store.get_settings(1).announce is True
        assert it.response.send_message.call_args.kwargs["ephemeral"] is True

    async def test_settings_channel_without_permission(self, store):
        mgr = make_manager(store)
        it = make_interaction()
        await mgr.handle_settings(it, True, make_channel(can_send=False))
        assert store.get_settings(1).channel_id is None
        assert it.response.send_message.call_args.kwargs["ephemeral"] is True


# ───────────── 보상 역할 ─────────────

class TestRewards:
    async def test_grant_on_level_up(self, store):
        role = FakeRole(50, position=5)
        guild, author = make_guild(roles=[role]), make_member()
        store.set_reward(1, 1, 50)
        await _level_up(store, guild, author, make_channel())
        author.add_roles.assert_awaited_once()
        assert author.add_roles.call_args.args == (role,)

    async def test_catch_up_lower_levels_and_skip_owned(self, store):
        r1, r2, r3 = FakeRole(51, 3), FakeRole(52, 4), FakeRole(53, 5)
        guild = make_guild(roles=[r1, r2, r3])
        author = make_member(roles=[r1])
        for lvl, r in ((1, r1), (2, r2), (5, r3)):
            store.set_reward(1, lvl, r.id)
        mgr = make_manager(store)
        added = await mgr.grant_rewards(guild, author, 3)
        assert added == [52]

    async def test_higher_role_not_granted(self, store):
        high = FakeRole(60, position=20)
        guild, author = make_guild(roles=[high]), make_member()
        store.set_reward(1, 1, 60)
        await _level_up(store, guild, author, make_channel())
        author.add_roles.assert_not_awaited()

    async def test_deleted_role_skipped(self, store):
        guild, author = make_guild(), make_member()
        store.set_reward(1, 1, 999999)
        await _level_up(store, guild, author, make_channel())
        author.add_roles.assert_not_awaited()

    async def test_add_roles_forbidden_is_swallowed(self, store):
        role = FakeRole(50, 5)
        guild, author = make_guild(roles=[role]), make_member()
        author.add_roles.side_effect = discord.Forbidden(MagicMock(status=403), "no")
        store.set_reward(1, 1, 50)
        _, r = await _level_up(store, guild, author, make_channel())
        assert r.leveled_up

    async def test_reward_command_ok(self, store):
        mgr = make_manager(store)
        it = make_interaction()
        role = FakeRole(50, 5)
        await mgr.handle_reward(it, 5, role)
        assert store.rewards_up_to(1, 5) == [(5, 50)]
        assert it.response.send_message.call_args.kwargs["ephemeral"] is True

    @pytest.mark.parametrize("role,me", [
        (FakeRole(50, 10), make_me(10)),             # 같은 위치
        (FakeRole(50, 20), make_me(10)),             # 더 높음
        (FakeRole(50, 3, managed=True), make_me()),  # 관리형 역할
        (FakeRole(1, 0, default=True), make_me()),   # @everyone
        (FakeRole(50, 3), make_me(10, manage_roles=False)),  # 역할 관리 권한 없음
    ])
    async def test_reward_command_rejects(self, store, role, me):
        mgr = make_manager(store)
        it = make_interaction(guild=make_guild(me=me))
        await mgr.handle_reward(it, 5, role)
        assert store.rewards_up_to(1, 1000) == []
        args, kwargs = it.response.send_message.call_args
        assert kwargs["ephemeral"] is True and "⚠️" in args[0]

    async def test_reward_command_requires_admin(self, store):
        mgr = make_manager(store)
        it = make_interaction(admin=False)
        await mgr.handle_reward(it, 5, FakeRole(50, 5))
        assert store.rewards_up_to(1, 1000) == []

    async def test_reward_level_range(self, store):
        mgr = make_manager(store)
        it = make_interaction()
        await mgr.handle_reward(it, 0, FakeRole(50, 5))
        assert store.rewards_up_to(1, 1000) == []

    def test_reward_overwrite_and_limit(self, store):
        assert store.set_reward(1, 5, 50)
        assert store.set_reward(1, 5, 51)
        assert store.rewards_up_to(1, 5) == [(5, 51)]
        for lvl in range(6, 6 + lv.MAX_REWARDS_PER_GUILD - 1):
            assert store.set_reward(1, lvl, 1000 + lvl)
        assert store.set_reward(1, 500, 7) is False
        assert store.set_reward(1, 5, 52) is True  # 기존 레벨 덮어쓰기는 허용


# ───────────── 서버 격리 ─────────────

class TestIsolation:
    def test_xp_isolated(self, store):
        store.add_xp(1, 100, 50, now=0)
        store.add_xp(2, 100, 20, now=0)
        assert store.get_xp(1, 100) == 50 and store.get_xp(2, 100) == 20
        assert store.top(2) == [(100, 20)]

    def test_settings_isolated(self, store):
        store.set_settings(1, False, 5)
        assert store.get_settings(2).announce is True
        assert store.get_settings(2).channel_id is None

    def test_rewards_isolated(self, store):
        store.set_reward(1, 1, 50)
        assert store.rewards_up_to(2, 100) == []

    def test_rank_isolated(self, store):
        store.add_xp(1, 100, 10, now=0)
        store.add_xp(2, 200, 99, now=0)
        assert store.get_rank(1, 100) == 1
        assert store.get_rank(2, 100) is None

    def test_env_db_path(self, tmp_path, monkeypatch):
        path = tmp_path / "sub" / "env.db"
        monkeypatch.setenv(lv.DB_PATH_ENV, str(path))
        s = lv.LevelStore()
        assert s.db_path == str(path) and not path.exists()  # 생성자만으로는 파일을 만들지 않음
        s.get_settings(1)
        assert path.exists()


# ───────────── 랭킹 ─────────────

class TestRanking:
    def test_sorted_desc_tiebreak_user_id(self, store):
        for uid, xp in ((1, 50), (2, 300), (3, 50), (4, 120)):
            store.add_xp(1, uid, xp, now=0)
        assert store.top(1) == [(2, 300), (4, 120), (1, 50), (3, 50)]
        assert [store.get_rank(1, u) for u in (2, 4, 1, 3)] == [1, 2, 3, 4]

    def test_limit_10(self, store):
        for uid in range(1, 16):
            store.add_xp(1, uid, uid * 10, now=0)
        top = store.top(1)
        assert len(top) == 10 and top[0] == (15, 150) and top[-1] == (6, 60)

    async def test_ranking_command_no_ping(self, store):
        store.add_xp(1, 7, 300, now=0)
        mgr = make_manager(store)
        it = make_interaction()
        await mgr.handle_ranking(it)
        kwargs = it.response.send_message.call_args.kwargs
        assert "<@7>" in kwargs["embed"].description and "레벨 2" in kwargs["embed"].description
        assert kwargs["allowed_mentions"].users is False

    async def test_ranking_empty_is_ephemeral(self, store):
        mgr = make_manager(store)
        it = make_interaction()
        await mgr.handle_ranking(it)
        assert it.response.send_message.call_args.kwargs["ephemeral"] is True


# ───────────── /레벨 ─────────────

class TestLevelCommand:
    async def test_rank_card_png(self, store):
        store.add_xp(1, 100, 130, now=0)
        mgr = make_manager(store)
        it = make_interaction()
        await mgr.handle_level(it)
        f = it.response.send_message.call_args.kwargs["file"]
        assert f.filename == "rank.png"
        assert f.fp.read(8) == b"\x89PNG\r\n\x1a\n"

    async def test_embed_fallback(self, store, monkeypatch):
        monkeypatch.setattr(lv, "render_rank_card", lambda *a, **k: None)
        store.add_xp(1, 100, 130, now=0)
        mgr = make_manager(store)
        it = make_interaction()
        await mgr.handle_level(it)
        embed = it.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        assert values["레벨"] == "1" and values["서버 순위"] == "#1"
        assert "30 / 155 XP" in values["다음 레벨까지"]

    async def test_bot_target_ephemeral(self, store):
        mgr = make_manager(store)
        it = make_interaction()
        await mgr.handle_level(it, make_member(bot=True))
        assert it.response.send_message.call_args.kwargs["ephemeral"] is True

    def test_render_without_font_returns_none(self, monkeypatch):
        monkeypatch.setattr(lv, "FONT_BOLD", "/nonexistent.ttf")
        assert lv.render_rank_card("a", 1, 1, 1, 1, 1) is None


# ───────────── 등록 ─────────────

def test_register_adds_commands_and_listener(tmp_path):
    bot = MagicMock()
    mgr = lv.register(bot, lv.LevelStore(str(tmp_path / "r.db")))
    names = {c.args[0].name for c in bot.tree.add_command.call_args_list}
    assert names == {"레벨", "레벨랭킹", "레벨설정", "레벨보상"}
    bot.add_listener.assert_called_once_with(mgr.on_message, "on_message")
    assert bot.level_manager is mgr
