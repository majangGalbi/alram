"""채팅 레벨·경험치 기능 (/레벨, /레벨랭킹, /레벨설정, /레벨보상).

bot.py 에서는 ``leveling.register(bot)`` 한 줄로만 연결한다.
메시지 이벤트(on_message)는 ``bot.add_listener`` 로 붙이므로 다른 기능의 리스너와 충돌하지 않는다.

설계
----
- 메시지 **내용은 읽지 않는다**. 작성자·서버·채널 정보만 쓰므로 message_content 인텐트가 필요 없다
  (기본 인텐트의 guild_messages 만 있으면 on_message 가 온다).
- 서버 메시지 1건당 15~25 XP 를 주되 사용자별 60초 쿨다운. 봇·웹훅·DM 메시지는 제외.
- 레벨 L 에서 L+1 로 가는 데 필요한 XP = 5*L^2 + 50*L + 100 (누적 XP 로 저장).
- 데이터는 SQLite(환경변수 ``LEVELING_DB_PATH``, 기본 ``leveling.db``), 모든 테이블은 guild_id 로 격리.
  DB 작업은 짧은 트랜잭션으로 ``asyncio.to_thread`` 에서 실행해 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

import asyncio
import io
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import discord
from discord import app_commands

__all__ = [
    "register", "LevelStore", "LevelManager", "xp_to_next", "total_xp_for_level", "level_from_xp",
    "level_progress", "render_rank_card",
]

DB_PATH_ENV = "LEVELING_DB_PATH"
DEFAULT_DB_PATH = "leveling.db"

XP_MIN = 15
XP_MAX = 25
COOLDOWN_SECONDS = 60
RANKING_LIMIT = 10
MAX_REWARD_LEVEL = 1000
MAX_REWARDS_PER_GUILD = 50
BAR_WIDTH = 12

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "NanumGothic-Regular.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "NanumGothic-Bold.ttf")

NO_MENTIONS = discord.AllowedMentions.none()


# ─────────────────────────────────────────────────────────────────────────────
# 레벨 공식
# ─────────────────────────────────────────────────────────────────────────────

def xp_to_next(level: int) -> int:
    """레벨 ``level`` 에서 다음 레벨로 가는 데 필요한 XP."""
    if level < 0:
        raise ValueError("level 은 0 이상이어야 합니다.")
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """레벨 ``level`` 에 도달하는 데 필요한 누적 XP (레벨 0 = 0)."""
    if level < 0:
        raise ValueError("level 은 0 이상이어야 합니다.")
    # sum_{i=0}^{L-1} (5i^2 + 50i + 100)
    n = level
    return 5 * (n - 1) * n * (2 * n - 1) // 6 + 50 * (n - 1) * n // 2 + 100 * n


def level_from_xp(xp: int) -> int:
    """누적 XP 로 현재 레벨 계산."""
    if xp < 0:
        return 0
    level = 0
    while total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


def level_progress(xp: int) -> Tuple[int, int, int]:
    """(레벨, 현재 레벨 안에서 쌓은 XP, 다음 레벨까지 필요한 XP 구간 크기)."""
    level = level_from_xp(xp)
    return level, xp - total_xp_for_level(level), xp_to_next(level)


def progress_bar(current: int, needed: int, width: int = BAR_WIDTH) -> str:
    ratio = 0.0 if needed <= 0 else max(0.0, min(1.0, current / needed))
    filled = int(ratio * width)
    return "▰" * filled + "▱" * (width - filled)


# ─────────────────────────────────────────────────────────────────────────────
# 저장소
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class XpResult:
    xp: int
    old_level: int
    new_level: int
    gained: int

    @property
    def leveled_up(self) -> bool:
        return self.new_level > self.old_level


@dataclass
class GuildSettings:
    announce: bool = True
    channel_id: Optional[int] = None


class LevelStore:
    """동기 SQLite 저장소. 호출부(LevelManager)가 asyncio.to_thread 로 감싼다."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv(DB_PATH_ENV) or DEFAULT_DB_PATH
        self._initialized = False  # DB 파일·테이블은 첫 사용 시 생성 (import 만으로 파일을 만들지 않음)

    def _connect(self) -> sqlite3.Connection:
        if not self._initialized:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 10000")
        if not self._initialized:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS levels (
                    guild_id INTEGER NOT NULL,
                    user_id  INTEGER NOT NULL,
                    xp       INTEGER NOT NULL DEFAULT 0,
                    last_ts  REAL    NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_levels_rank ON levels (guild_id, xp DESC);
                CREATE TABLE IF NOT EXISTS settings (
                    guild_id   INTEGER PRIMARY KEY,
                    announce   INTEGER NOT NULL DEFAULT 1,
                    channel_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS rewards (
                    guild_id INTEGER NOT NULL,
                    level    INTEGER NOT NULL,
                    role_id  INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, level)
                );
                """
            )
            self._initialized = True
        return conn

    # XP ---------------------------------------------------------------------
    def add_xp(self, guild_id: int, user_id: int, amount: int, now: float,
               cooldown: float = COOLDOWN_SECONDS) -> Optional[XpResult]:
        """쿨다운이 지났으면 XP 를 더하고 결과를, 쿨다운 중이면 None 을 돌려준다."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT xp, last_ts FROM levels WHERE guild_id=? AND user_id=?",
                               (guild_id, user_id)).fetchone()
            old_xp, last_ts = (row if row else (0, None))
            if last_ts is not None and now - last_ts < cooldown:
                conn.execute("ROLLBACK")
                return None
            new_xp = old_xp + amount
            conn.execute(
                "INSERT INTO levels (guild_id, user_id, xp, last_ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, last_ts=excluded.last_ts",
                (guild_id, user_id, new_xp, now))
            conn.execute("COMMIT")
            return XpResult(new_xp, level_from_xp(old_xp), level_from_xp(new_xp), amount)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_xp(self, guild_id: int, user_id: int) -> int:
        with self._closing() as conn:
            row = conn.execute("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                               (guild_id, user_id)).fetchone()
        return row[0] if row else 0

    def get_rank(self, guild_id: int, user_id: int) -> Optional[int]:
        """서버 내 순위(1부터). 기록이 없으면 None. 동점은 user_id 가 작은 쪽이 앞(랭킹과 동일)."""
        with self._closing() as conn:
            row = conn.execute("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                               (guild_id, user_id)).fetchone()
            if not row:
                return None
            ahead = conn.execute(
                "SELECT COUNT(*) FROM levels WHERE guild_id=? AND (xp > ? OR (xp = ? AND user_id < ?))",
                (guild_id, row[0], row[0], user_id)).fetchone()[0]
        return ahead + 1

    def top(self, guild_id: int, limit: int = RANKING_LIMIT) -> List[Tuple[int, int]]:
        """[(user_id, xp)] — XP 내림차순, 동점은 user_id 오름차순."""
        with self._closing() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT user_id, xp FROM levels WHERE guild_id=? AND xp > 0 "
                "ORDER BY xp DESC, user_id ASC LIMIT ?", (guild_id, limit)).fetchall()]

    # 설정 --------------------------------------------------------------------
    def get_settings(self, guild_id: int) -> GuildSettings:
        with self._closing() as conn:
            row = conn.execute("SELECT announce, channel_id FROM settings WHERE guild_id=?",
                               (guild_id,)).fetchone()
        return GuildSettings(bool(row[0]), row[1]) if row else GuildSettings()

    def set_settings(self, guild_id: int, announce: bool, channel_id: Optional[int]) -> None:
        with self._closing() as conn:
            conn.execute(
                "INSERT INTO settings (guild_id, announce, channel_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET announce=excluded.announce, channel_id=excluded.channel_id",
                (guild_id, int(announce), channel_id))

    # 보상 역할 ----------------------------------------------------------------
    def set_reward(self, guild_id: int, level: int, role_id: int) -> bool:
        """보상 등록. 서버당 개수 제한을 넘는 새 레벨이면 False."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT 1 FROM rewards WHERE guild_id=? AND level=?",
                                  (guild_id, level)).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM rewards WHERE guild_id=?", (guild_id,)).fetchone()[0]
            if not exists and count >= MAX_REWARDS_PER_GUILD:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT INTO rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, level) DO UPDATE SET role_id=excluded.role_id",
                (guild_id, level, role_id))
            conn.execute("COMMIT")
            return True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def rewards_up_to(self, guild_id: int, level: int) -> List[Tuple[int, int]]:
        """[(level, role_id)] — level 이하 보상 전체 (레벨 오름차순)."""
        with self._closing() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT level, role_id FROM rewards WHERE guild_id=? AND level<=? ORDER BY level",
                (guild_id, level)).fetchall()]

    def _closing(self):
        store = self

        class _Ctx:
            def __enter__(self_inner):
                self_inner.conn = store._connect()
                return self_inner.conn

            def __exit__(self_inner, *exc):
                self_inner.conn.close()
                return False

        return _Ctx()


# ─────────────────────────────────────────────────────────────────────────────
# 랭크 카드 (Pillow)
# ─────────────────────────────────────────────────────────────────────────────

def render_rank_card(name: str, level: int, xp: int, current: int, needed: int,
                     rank: Optional[int]) -> Optional[bytes]:
    """랭크 카드 PNG 바이트. Pillow·폰트가 없거나 실패하면 None (임베드로 폴백)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        if not (os.path.exists(FONT_BOLD) and os.path.exists(FONT_REGULAR)):
            return None
        width, height = 800, 220
        img = Image.new("RGB", (width, height), (35, 39, 42))
        draw = ImageDraw.Draw(img)
        f_name = ImageFont.truetype(FONT_BOLD, 36)
        f_big = ImageFont.truetype(FONT_BOLD, 30)
        f_small = ImageFont.truetype(FONT_REGULAR, 22)

        right = f"레벨 {level}   " + (f"#{rank}" if rank else "순위 없음")
        tw = draw.textlength(right, font=f_big)
        draw.text((width - 40 - tw, 34), right, font=f_big, fill=(88, 196, 255))
        max_name = width - 80 - tw - 30  # 오른쪽 레벨/순위 글자와 겹치지 않게 폭 기준으로 자름
        if draw.textlength(name, font=f_name) > max_name:
            while name and draw.textlength(name + "…", font=f_name) > max_name:
                name = name[:-1]
            name += "…"
        draw.text((40, 30), name, font=f_name, fill=(255, 255, 255))

        x0, y0, x1, y1 = 40, 120, width - 40, 160
        draw.rounded_rectangle((x0, y0, x1, y1), radius=20, fill=(72, 75, 78))
        ratio = 0.0 if needed <= 0 else max(0.0, min(1.0, current / needed))
        if ratio > 0:
            fill_x = x0 + max(int((x1 - x0) * ratio), 40)
            draw.rounded_rectangle((x0, y0, fill_x, y1), radius=20, fill=(88, 196, 255))
        draw.text((40, 172), f"{current:,} / {needed:,} XP", font=f_small, fill=(200, 200, 200))
        total = f"누적 {xp:,} XP"
        tw = draw.textlength(total, font=f_small)
        draw.text((width - 40 - tw, 172), total, font=f_small, fill=(200, 200, 200))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001 - 어떤 이유든 임베드 폴백
        print(f"⚠️ 랭크 카드 생성 실패, 임베드로 대체: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 매니저 (이벤트·명령 처리)
# ─────────────────────────────────────────────────────────────────────────────

def is_admin(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


async def send_private(interaction: discord.Interaction, content: str) -> None:
    """본인에게만 보이는 안내. 아직 응답 전일 때만 쓴다(공개 defer 뒤 followup 은 공개로 보이므로)."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS)


class LevelManager:
    def __init__(self, store: LevelStore, rng: Optional[random.Random] = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.rng = rng or random.Random()
        self.clock = clock

    # 메시지 → XP ------------------------------------------------------------
    async def on_message(self, message: discord.Message) -> Optional[XpResult]:
        author = message.author
        guild = message.guild
        if guild is None or getattr(author, "bot", False) or getattr(message, "webhook_id", None):
            return None
        amount = self.rng.randint(XP_MIN, XP_MAX)
        try:
            result = await asyncio.to_thread(self.store.add_xp, guild.id, author.id, amount, self.clock())
        except sqlite3.Error as e:
            print(f"⚠️ 레벨 XP 저장 실패: {e}")
            return None
        if result is None or not result.leveled_up:
            return result
        await self.grant_rewards(guild, author, result.new_level)
        await self.announce(message, result.new_level)
        return result

    async def grant_rewards(self, guild, member, level: int) -> List[int]:
        """level 이하 보상 역할 중 아직 없는 것을 지급. 지급한 role_id 목록."""
        rewards = await asyncio.to_thread(self.store.rewards_up_to, guild.id, level)
        if not rewards or not hasattr(member, "add_roles"):
            return []
        me = guild.me
        have = {r.id for r in getattr(member, "roles", [])}
        to_add = []
        for _lvl, role_id in rewards:
            role = guild.get_role(role_id)
            if role is None or role.id in have or role_problem(role, me):
                continue
            to_add.append(role)
        if not to_add:
            return []
        try:
            await member.add_roles(*to_add, reason=f"레벨 {level} 달성 보상")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"⚠️ 레벨 보상 역할 지급 실패 (guild={guild.id}): {e}")
            return []
        return [r.id for r in to_add]

    async def announce(self, message, level: int) -> bool:
        guild = message.guild
        settings = await asyncio.to_thread(self.store.get_settings, guild.id)
        if not settings.announce:
            return False
        channel = message.channel
        if settings.channel_id:
            channel = guild.get_channel(settings.channel_id) or channel
        me = guild.me
        try:
            perms = channel.permissions_for(me)
            if not perms.send_messages:
                return False
        except Exception:  # noqa: BLE001 - 권한 조회 불가한 채널 타입은 그냥 시도
            pass
        author = message.author
        try:
            await channel.send(
                f"🎉 {author.mention} 님이 **레벨 {level}** 에 도달했어요!",
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[author],
                                                         replied_user=False),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"⚠️ 레벨업 알림 전송 실패: {e}")
            return False
        return True

    # 명령 ---------------------------------------------------------------------
    async def handle_level(self, interaction: discord.Interaction, target=None) -> None:
        if interaction.guild_id is None:
            await send_private(interaction, "서버에서만 사용할 수 있는 명령입니다.")
            return
        user = target or interaction.user
        if getattr(user, "bot", False):
            await send_private(interaction, "봇은 레벨이 없습니다.")
            return
        gid = interaction.guild_id
        xp = await asyncio.to_thread(self.store.get_xp, gid, user.id)
        rank = await asyncio.to_thread(self.store.get_rank, gid, user.id)
        level, current, needed = level_progress(xp)
        name = getattr(user, "display_name", str(user))
        png = await asyncio.to_thread(render_rank_card, name, level, xp, current, needed, rank)
        if png:
            await interaction.response.send_message(
                file=discord.File(io.BytesIO(png), filename="rank.png"), allowed_mentions=NO_MENTIONS)
            return
        embed = discord.Embed(title=f"📊 {name} 님의 레벨", color=0x58C4FF)
        embed.add_field(name="레벨", value=str(level))
        embed.add_field(name="누적 XP", value=f"{xp:,}")
        embed.add_field(name="서버 순위", value=f"#{rank}" if rank else "기록 없음")
        embed.add_field(name="다음 레벨까지",
                        value=f"{progress_bar(current, needed)} {current:,} / {needed:,} XP", inline=False)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    async def handle_ranking(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await send_private(interaction, "서버에서만 사용할 수 있는 명령입니다.")
            return
        rows = await asyncio.to_thread(self.store.top, interaction.guild_id, RANKING_LIMIT)
        if not rows:
            await send_private(interaction, "아직 레벨 기록이 없습니다. 채팅을 하면 경험치가 쌓여요!")
            return
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, (uid, xp) in enumerate(rows, start=1):
            lines.append(f"{medals.get(i, f'`{i}.`')} <@{uid}> — 레벨 {level_from_xp(xp)} ({xp:,} XP)")
        embed = discord.Embed(title="🏆 레벨 랭킹 TOP 10", description="\n".join(lines), color=0xFFD700)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    async def handle_settings(self, interaction: discord.Interaction, announce: bool, channel=None) -> None:
        if interaction.guild_id is None:
            await send_private(interaction, "서버에서만 사용할 수 있는 명령입니다.")
            return
        if not is_admin(interaction.user):
            await send_private(interaction, "🚫 서버 관리 권한이 있는 사람만 설정할 수 있습니다.")
            return
        if channel is not None and interaction.guild is not None:
            me = interaction.guild.me
            perms = channel.permissions_for(me) if me is not None else None
            if perms is not None and not (perms.view_channel and perms.send_messages):
                await send_private(interaction, f"⚠️ 봇이 {channel.mention} 채널에 메시지를 보낼 권한이 없습니다.")
                return
        channel_id = channel.id if channel is not None else None
        await asyncio.to_thread(self.store.set_settings, interaction.guild_id, announce, channel_id)
        if not announce:
            msg = "🔕 레벨업 알림을 껐습니다."
        elif channel is not None:
            msg = f"🔔 레벨업 알림을 켰습니다. 알림 채널: {channel.mention}"
        else:
            msg = "🔔 레벨업 알림을 켰습니다. (메시지를 보낸 채널에 알림)"
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def handle_reward(self, interaction: discord.Interaction, level: int, role) -> None:
        if interaction.guild_id is None or interaction.guild is None:
            await send_private(interaction, "서버에서만 사용할 수 있는 명령입니다.")
            return
        if not is_admin(interaction.user):
            await send_private(interaction, "🚫 서버 관리 권한이 있는 사람만 설정할 수 있습니다.")
            return
        if not 1 <= level <= MAX_REWARD_LEVEL:
            await send_private(interaction, f"레벨은 1~{MAX_REWARD_LEVEL} 사이로 입력해 주세요.")
            return
        problem = role_problem(role, interaction.guild.me)
        if problem:
            await send_private(interaction, f"⚠️ {role.mention} 역할은 보상으로 쓸 수 없습니다: {problem}")
            return
        ok = await asyncio.to_thread(self.store.set_reward, interaction.guild_id, level, role.id)
        if not ok:
            await send_private(interaction, f"보상은 서버당 최대 {MAX_REWARDS_PER_GUILD}개까지 등록할 수 있습니다.")
            return
        await interaction.response.send_message(
            f"🎁 레벨 {level} 에 도달하면 {role.mention} 역할을 자동으로 지급합니다.",
            ephemeral=True, allowed_mentions=NO_MENTIONS)


def role_problem(role, me) -> Optional[str]:
    """보상 역할로 쓸 수 없는 이유. 문제없으면 None."""
    if getattr(role, "is_default", lambda: False)():
        return "@everyone 역할입니다."
    if getattr(role, "managed", False):
        return "봇/연동 서비스가 관리하는 역할입니다."
    if me is None:
        return "봇 정보를 확인할 수 없습니다."
    perms = getattr(me, "guild_permissions", None)
    if perms is not None and not (perms.manage_roles or perms.administrator):
        return "봇에게 '역할 관리' 권한이 없습니다."
    top = getattr(me, "top_role", None)
    if top is None or not role < top:
        return "봇의 가장 높은 역할보다 같거나 높은 위치에 있습니다. 서버 설정 → 역할에서 봇 역할을 더 위로 옮겨 주세요."
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 등록
# ─────────────────────────────────────────────────────────────────────────────

ANNOUNCE_CHOICES = [app_commands.Choice(name="켜기", value="on"), app_commands.Choice(name="끄기", value="off")]


def register(bot, store: Optional[LevelStore] = None) -> LevelManager:
    """명령 4개를 bot.tree 에 등록하고 on_message 리스너를 추가한다."""
    manager = LevelManager(store or LevelStore())
    bot.level_manager = manager

    @app_commands.command(name="레벨", description="채팅 레벨·경험치와 서버 순위를 보여줍니다.")
    @app_commands.describe(사용자="조회할 사용자 (비우면 본인)")
    @app_commands.guild_only()
    async def level_cmd(interaction: discord.Interaction, 사용자: Optional[discord.Member] = None):
        await manager.handle_level(interaction, 사용자)

    @app_commands.command(name="레벨랭킹", description="서버 채팅 레벨 상위 10명을 보여줍니다.")
    @app_commands.guild_only()
    async def ranking_cmd(interaction: discord.Interaction):
        await manager.handle_ranking(interaction)

    @app_commands.command(name="레벨설정", description="레벨업 알림을 켜거나 끄고, 알림 채널을 지정합니다. (서버 관리 권한)")
    @app_commands.describe(레벨업알림="레벨업 축하 메시지 켜기/끄기", 채널="(선택) 알림을 보낼 채널. 비우면 메시지를 보낸 채널")
    @app_commands.choices(레벨업알림=ANNOUNCE_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def settings_cmd(interaction: discord.Interaction, 레벨업알림: app_commands.Choice[str],
                           채널: Optional[discord.TextChannel] = None):
        await manager.handle_settings(interaction, 레벨업알림.value == "on", 채널)

    @app_commands.command(name="레벨보상", description="특정 레벨 도달 시 자동 지급할 역할을 설정합니다. (서버 관리 권한)")
    @app_commands.describe(레벨="보상을 줄 레벨 (1 이상)", 역할="지급할 역할 (봇 역할보다 아래여야 함)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def reward_cmd(interaction: discord.Interaction, 레벨: app_commands.Range[int, 1, MAX_REWARD_LEVEL],
                         역할: discord.Role):
        await manager.handle_reward(interaction, 레벨, 역할)

    for cmd in (level_cmd, ranking_cmd, settings_cmd, reward_cmd):
        bot.tree.add_command(cmd)
    bot.add_listener(manager.on_message, "on_message")
    return manager
