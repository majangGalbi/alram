"""버튼 투표 기능 (/투표, /투표마감).

bot.py 에서는 다음 두 가지만 호출한다.

- ``polls.register(bot)``  : 슬래시 명령(/투표, /투표마감)을 bot.tree 에 등록
- ``await polls.restore(bot)`` : setup_hook 에서 호출. SQLite 에 저장된 진행 중 투표의
  버튼(persistent view)과 자동 마감 타이머(asyncio 태스크)를 복원

설계
----
- 투표 데이터는 SQLite 파일(환경변수 ``POLLS_DB_PATH``, 기본 ``polls.db``)에 저장한다.
- 버튼 custom_id 는 ``alram-poll:<항목번호>`` 로 고정하고 ``timeout=None`` 인 View 를
  ``bot.add_view`` 로 등록한다. 어떤 투표인지는 버튼이 달린 메시지 ID 로 구분하므로
  봇이 재시작되어도 기존 메시지의 버튼이 그대로 동작한다.
- 자동 마감은 APScheduler 가 아닌 asyncio 태스크(투표별 1개)로 처리한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import discord
import pytz
from discord import app_commands

from time_parser import TimeParseError, parse_time

__all__ = ["register", "restore", "PollManager", "Poll", "PollStore"]

SEOUL = pytz.timezone("Asia/Seoul")

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polls.db")  # 실행 위치(cwd)와 무관하게 bot.py 옆에 저장
DB_PATH_ENV = "POLLS_DB_PATH"

CUSTOM_ID_PREFIX = "alram-poll:"
MIN_OPTIONS = 2
MAX_OPTIONS = 10                 # 버튼 2줄(5개씩) 이내
MAX_OPTION_LENGTH = 80           # 디스코드 버튼 라벨 최대 80자
MAX_QUESTION_LENGTH = 200        # 임베드 제목 256자 제한 안쪽 (접두 이모지 포함)
MAX_OPEN_POLLS_PER_GUILD = 50    # 서버당 진행 중 투표 수 제한 (남용 방지)
MAX_POLL_DURATION = timedelta(days=30)
BAR_WIDTH = 10
EMBED_TOTAL_LIMIT = 6000         # 디스코드 임베드 전체 글자 수 제한
EMBED_FIELD_VALUE_LIMIT = 1024
VOTER_FIELD_LIMIT = 300          # 공개 투표 시 항목별 투표자 목록 길이 (전체 6000자 안쪽 유지)

# 질문/항목에 들어간 @everyone, 역할/사용자 멘션이 절대 울리지 않도록
NO_MENTIONS = discord.AllowedMentions.none()


# ─────────────────────────────────────────────────────────────────────────────
# 데이터 / 저장소
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Poll:
    message_id: int
    guild_id: int
    channel_id: int
    creator_id: int
    question: str
    options: List[str]
    allow_multiple: bool = False
    anonymous: bool = True
    closes_at: Optional[float] = None   # UTC epoch 초, None 이면 수동 마감만
    closed: bool = False
    votes: Dict[int, List[int]] = field(default_factory=dict)  # user_id -> [항목 번호]

    def counts(self) -> List[int]:
        result = [0] * len(self.options)
        for choices in self.votes.values():
            for idx in choices:
                if 0 <= idx < len(result):
                    result[idx] += 1
        return result

    def voters_of(self, idx: int) -> List[int]:
        return sorted(uid for uid, choices in self.votes.items() if idx in choices)

    @property
    def participants(self) -> int:
        return sum(1 for choices in self.votes.values() if choices)

    def winners(self) -> List[int]:
        """최다 득표 항목 번호 목록 (동점이면 여러 개, 표가 없으면 빈 목록)."""
        counts = self.counts()
        top = max(counts) if counts else 0
        if top == 0:
            return []
        return [i for i, c in enumerate(counts) if c == top]


class PollStore:
    """SQLite 저장소. 단일 이벤트 루프에서만 호출되므로 동기 sqlite3 로 충분하다."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS polls (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    creator_id INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    options TEXT NOT NULL,
                    allow_multiple INTEGER NOT NULL,
                    anonymous INTEGER NOT NULL,
                    closes_at REAL,
                    closed INTEGER NOT NULL DEFAULT 0
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS poll_votes (
                    message_id INTEGER NOT NULL REFERENCES polls(message_id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    option_idx INTEGER NOT NULL,
                    PRIMARY KEY (message_id, user_id, option_idx)
                )"""
            )

    def close_connection(self):
        self.conn.close()

    def add_poll(self, poll: Poll):
        with self.conn:
            self.conn.execute(
                "INSERT INTO polls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (poll.message_id, poll.guild_id, poll.channel_id, poll.creator_id, poll.question,
                 json.dumps(poll.options, ensure_ascii=False), int(poll.allow_multiple),
                 int(poll.anonymous), poll.closes_at, int(poll.closed)),
            )

    def _row_to_poll(self, row) -> Poll:
        poll = Poll(
            message_id=row[0], guild_id=row[1], channel_id=row[2], creator_id=row[3],
            question=row[4], options=json.loads(row[5]), allow_multiple=bool(row[6]),
            anonymous=bool(row[7]), closes_at=row[8], closed=bool(row[9]),
        )
        for user_id, idx in self.conn.execute(
            "SELECT user_id, option_idx FROM poll_votes WHERE message_id = ? ORDER BY rowid",
            (poll.message_id,),
        ):
            poll.votes.setdefault(user_id, []).append(idx)
        return poll

    def get_poll(self, message_id: int) -> Optional[Poll]:
        row = self.conn.execute("SELECT * FROM polls WHERE message_id = ?", (message_id,)).fetchone()
        return self._row_to_poll(row) if row else None

    def open_polls(self) -> List[Poll]:
        rows = self.conn.execute("SELECT * FROM polls WHERE closed = 0").fetchall()
        return [self._row_to_poll(r) for r in rows]

    def count_open_in_guild(self, guild_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM polls WHERE closed = 0 AND guild_id = ?", (guild_id,)
        ).fetchone()[0]

    def toggle_vote(self, message_id: int, user_id: int, idx: int, allow_multiple: bool) -> str:
        """투표/변경/취소를 한 트랜잭션으로 처리. 반환값: 'added' | 'changed' | 'removed'."""
        with self.conn:
            existing = [r[0] for r in self.conn.execute(
                "SELECT option_idx FROM poll_votes WHERE message_id = ? AND user_id = ?",
                (message_id, user_id),
            )]
            if idx in existing:
                self.conn.execute(
                    "DELETE FROM poll_votes WHERE message_id = ? AND user_id = ? AND option_idx = ?",
                    (message_id, user_id, idx),
                )
                return "removed"
            action = "added"
            if not allow_multiple and existing:
                self.conn.execute(
                    "DELETE FROM poll_votes WHERE message_id = ? AND user_id = ?", (message_id, user_id)
                )
                action = "changed"
            self.conn.execute(
                "INSERT INTO poll_votes (message_id, user_id, option_idx) VALUES (?, ?, ?)",
                (message_id, user_id, idx),
            )
            return action

    def mark_closed(self, message_id: int) -> bool:
        """처음으로 마감 처리한 경우에만 True (중복 결과 발표 방지)."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE polls SET closed = 1 WHERE message_id = ? AND closed = 0", (message_id,)
            )
            return cur.rowcount == 1


# ─────────────────────────────────────────────────────────────────────────────
# 입력 검증 / 렌더링
# ─────────────────────────────────────────────────────────────────────────────

class PollInputError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 한국어 메시지."""


def parse_options(text: str) -> List[str]:
    options = [part.strip() for part in text.replace("，", ",").split(",")]
    options = [o for o in options if o]
    if len(options) < MIN_OPTIONS:
        raise PollInputError(f"항목은 쉼표(,)로 구분해 {MIN_OPTIONS}개 이상 입력해 주세요. 예: `짜장, 짬뽕, 볶음밥`")
    if len(options) > MAX_OPTIONS:
        raise PollInputError(f"항목은 최대 {MAX_OPTIONS}개까지 입력할 수 있습니다. (입력: {len(options)}개)")
    for o in options:
        if len(o) > MAX_OPTION_LENGTH:
            raise PollInputError(f"각 항목은 {MAX_OPTION_LENGTH}자 이하로 입력해 주세요.")
    seen = set()
    for o in options:
        key = o.casefold()
        if key in seen:
            raise PollInputError(f"중복된 항목이 있습니다: `{discord.utils.escape_markdown(o)}`")
        seen.add(key)
    return options


def validate_question(text: str) -> str:
    q = text.strip()
    if not q:
        raise PollInputError("질문을 입력해 주세요.")
    if len(q) > MAX_QUESTION_LENGTH:
        raise PollInputError(f"질문은 {MAX_QUESTION_LENGTH}자 이하로 입력해 주세요.")
    return q


def parse_deadline(text: Optional[str], now: datetime) -> Optional[float]:
    """마감 텍스트 → UTC epoch. ``now`` 는 Asia/Seoul aware datetime."""
    if text is None or not text.strip():
        return None
    try:
        parsed = parse_time(text, now.replace(tzinfo=None))
    except TimeParseError as e:
        raise PollInputError(f"마감 시간을 인식할 수 없습니다: {e}") from e
    deadline = SEOUL.localize(parsed)
    if deadline <= now:
        raise PollInputError("마감 시간은 현재 시간 이후여야 합니다.")
    if deadline - now > MAX_POLL_DURATION:
        raise PollInputError(f"마감 시간은 최대 {MAX_POLL_DURATION.days}일 이내로 지정해 주세요.")
    return deadline.timestamp()


def make_bar(count: int, total: int, width: int = BAR_WIDTH) -> str:
    if total <= 0:
        filled = 0
    else:
        filled = round(count / total * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def percent(count: int, total: int) -> float:
    return (count / total * 100) if total else 0.0


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe(text: str) -> str:
    """임베드/메시지에 표시할 사용자 입력: 마크다운·멘션 무력화."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def build_embed(poll: Poll) -> discord.Embed:
    counts = poll.counts()
    total = poll.participants
    winners = set(poll.winners()) if poll.closed else set()

    lines = []
    for i, (opt, c) in enumerate(zip(poll.options, counts, strict=True)):
        crown = "🏆 " if i in winners else ""
        lines.append(
            f"{crown}**{i + 1}. {_safe(opt)}**\n`{make_bar(c, total)}` {c}표 ({percent(c, total):.1f}%)"
        )

    meta = [f"👤 만든 사람: <@{poll.creator_id}>"]
    if poll.closes_at is not None:
        ts = int(poll.closes_at)
        meta.append(f"⏰ 마감: <t:{ts}:F> (<t:{ts}:R>)" if not poll.closed else f"⏰ 마감 시각: <t:{ts}:F>")
    description = "\n".join(lines) + "\n\n" + "\n".join(meta)

    status = "🔒 마감됨" if poll.closed else "🗳️ 진행 중"
    embed = discord.Embed(
        title=_clip(f"📊 {poll.question}", 256),
        description=_clip(description, 4096),
        color=discord.Color.dark_grey() if poll.closed else discord.Color.blurple(),
    )
    mode = "중복 선택 가능" if poll.allow_multiple else "1개만 선택"
    secrecy = "익명" if poll.anonymous else "공개"
    embed.set_footer(text=f"{status} · 참여 {total}명 · {mode} · {secrecy} 투표 · 같은 버튼을 다시 누르면 취소")

    if not poll.anonymous:
        for i, opt in enumerate(poll.options):
            voters = poll.voters_of(i)
            if not voters:
                continue
            value = _clip(", ".join(f"<@{uid}>" for uid in voters), VOTER_FIELD_LIMIT)
            embed.add_field(name=_clip(f"{i + 1}. {opt}", 256), value=value, inline=False)

    # 안전장치: 전체 6000자를 넘으면 투표자 필드부터 제거
    while len(embed) > EMBED_TOTAL_LIMIT and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
    return embed


def result_text(poll: Poll) -> str:
    counts = poll.counts()
    winners = poll.winners()
    q = _safe(_clip(poll.question, 100))
    link = f"https://discord.com/channels/{poll.guild_id}/{poll.channel_id}/{poll.message_id}"
    if not winners:
        body = "참여한 사람이 없어 결과가 없습니다."
    elif len(winners) == 1:
        w = winners[0]
        body = f"🏆 **{_safe(poll.options[w])}** ({counts[w]}표, {percent(counts[w], poll.participants):.1f}%)"
    else:
        names = ", ".join(f"**{_safe(poll.options[w])}**" for w in winners)
        body = f"🤝 동점: {names} (각 {counts[winners[0]]}표)"
    return f"📢 투표 「{q}」가 마감되었습니다! (참여 {poll.participants}명)\n{body}\n{link}"


# ─────────────────────────────────────────────────────────────────────────────
# 버튼 (persistent view)
# ─────────────────────────────────────────────────────────────────────────────

def custom_id_for(idx: int) -> str:
    return f"{CUSTOM_ID_PREFIX}{idx}"


class PollButton(discord.ui.Button):
    def __init__(self, manager: "PollManager", idx: int, label: str, disabled: bool = False):
        super().__init__(
            label=_clip(label, MAX_OPTION_LENGTH),
            custom_id=custom_id_for(idx),
            style=discord.ButtonStyle.secondary,
            row=idx // 5,
            disabled=disabled,
        )
        self.manager = manager
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        await self.manager.handle_vote(interaction, self.idx)


class PollView(discord.ui.View):
    """labels=None 이면 재시작 복원용 템플릿(항목 0~9 전부 등록)."""

    def __init__(self, manager: "PollManager", labels: Optional[List[str]] = None, disabled: bool = False):
        super().__init__(timeout=None)
        if labels is None:
            labels = [str(i + 1) for i in range(MAX_OPTIONS)]
        for i, label in enumerate(labels):
            self.add_item(PollButton(manager, i, label, disabled))


def _button_label(idx: int, option: str) -> str:
    return _clip(f"{idx + 1}. {option}", MAX_OPTION_LENGTH)


def make_view(manager: "PollManager", poll: Poll, disabled: bool = False) -> PollView:
    labels = [_button_label(i, opt) for i, opt in enumerate(poll.options)]
    return PollView(manager, labels=labels, disabled=disabled)


# ─────────────────────────────────────────────────────────────────────────────
# 핵심 로직
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


class PollManager:
    def __init__(self, bot, db_path: Optional[str] = None, clock=time.time):
        self.bot = bot
        self.db_path = db_path or os.getenv(DB_PATH_ENV) or DEFAULT_DB_PATH
        self._store: Optional[PollStore] = None
        self.tasks: Dict[int, asyncio.Task] = {}
        self.locks: Dict[int, asyncio.Lock] = {}
        self.clock = clock
        self.template_view_added = False

    @property
    def store(self) -> PollStore:
        # import 시점에 파일을 만들지 않도록 처음 쓸 때 연다
        if self._store is None:
            self._store = PollStore(self.db_path)
        return self._store

    def _lock(self, message_id: int) -> asyncio.Lock:
        return self.locks.setdefault(message_id, asyncio.Lock())

    # ── 생성 ──────────────────────────────────────────────────────────────
    async def handle_create(self, interaction, question: str, options_text: str,
                            deadline_text: Optional[str] = None,
                            allow_multiple: bool = False, anonymous: bool = True):
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ 투표는 서버 채널에서만 만들 수 있습니다.", ephemeral=True)
            return None
        try:
            q = validate_question(question)
            options = parse_options(options_text)
            closes_at = parse_deadline(deadline_text, datetime.fromtimestamp(self.clock(), SEOUL))
        except PollInputError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return None
        if self.store.count_open_in_guild(interaction.guild_id) >= MAX_OPEN_POLLS_PER_GUILD:
            await interaction.response.send_message(
                f"❌ 이 서버에서 진행 중인 투표가 너무 많습니다(최대 {MAX_OPEN_POLLS_PER_GUILD}개). 기존 투표를 먼저 마감해 주세요.",
                ephemeral=True,
            )
            return None

        poll = Poll(
            message_id=0, guild_id=interaction.guild_id, channel_id=interaction.channel_id,
            creator_id=interaction.user.id, question=q, options=options,
            allow_multiple=bool(allow_multiple), anonymous=bool(anonymous), closes_at=closes_at,
        )
        await interaction.response.send_message(
            embed=build_embed(poll), view=make_view(self, poll), allowed_mentions=NO_MENTIONS
        )
        message = await interaction.original_response()
        poll.message_id = message.id
        self.store.add_poll(poll)
        self.schedule_close(poll)
        return poll

    # ── 투표 ──────────────────────────────────────────────────────────────
    async def handle_vote(self, interaction, idx: int):
        message_id = interaction.message.id
        async with self._lock(message_id):
            poll = self.store.get_poll(message_id)
            if poll is None or poll.guild_id != interaction.guild_id:
                await interaction.response.send_message("❌ 이 투표 정보를 찾을 수 없습니다.", ephemeral=True)
                return None
            expired = poll.closes_at is not None and self.clock() >= poll.closes_at
            if poll.closed or expired:
                await interaction.response.send_message("🔒 이미 마감된 투표입니다.", ephemeral=True)
                if expired and not poll.closed:
                    asyncio.get_running_loop().create_task(self.close_poll(message_id))
                return None
            if not 0 <= idx < len(poll.options):
                await interaction.response.send_message("❌ 잘못된 항목입니다.", ephemeral=True)
                return None

            action = self.store.toggle_vote(message_id, interaction.user.id, idx, poll.allow_multiple)
            poll = self.store.get_poll(message_id)
            await interaction.response.edit_message(embed=build_embed(poll))

        name = _safe(poll.options[idx])
        text = {
            "added": f"✅ **{name}** 에 투표했습니다.",
            "changed": f"🔄 **{name}** (으)로 투표를 변경했습니다.",
            "removed": f"↩️ **{name}** 투표를 취소했습니다.",
        }[action]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        return action

    # ── 마감 ──────────────────────────────────────────────────────────────
    def schedule_close(self, poll: Poll):
        if poll.closes_at is None or poll.closed:
            return None
        old = self.tasks.pop(poll.message_id, None)
        if old is not None:
            old.cancel()
        task = asyncio.get_running_loop().create_task(self._close_later(poll.message_id, poll.closes_at))
        self.tasks[poll.message_id] = task
        return task

    async def _close_later(self, message_id: int, closes_at: float):
        try:
            # 아주 긴 sleep 도 시계 오차를 줄이도록 남은 시간을 다시 계산하며 대기
            while True:
                remaining = closes_at - self.clock()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 3600))
            self.tasks.pop(message_id, None)
            await self.close_poll(message_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 태스크 예외가 조용히 사라지지 않도록
            print(f"⚠️ 투표 자동 마감 실패 (message={message_id}): {type(e).__name__}: {e}")

    async def close_poll(self, message_id: int) -> bool:
        """마감 처리 + 메시지 버튼 비활성화 + 결과 발표. 이미 마감됐으면 False."""
        async with self._lock(message_id):
            if not self.store.mark_closed(message_id):
                return False
            poll = self.store.get_poll(message_id)
        task = self.tasks.pop(message_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

        try:
            channel = self.bot.get_channel(poll.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(poll.channel_id)
        except discord.HTTPException as e:
            print(f"⚠️ 투표 채널을 찾을 수 없음 (channel={poll.channel_id}): {e}")
            return True

        try:
            await channel.get_partial_message(message_id).edit(
                embed=build_embed(poll), view=make_view(self, poll, disabled=True)
            )
        except discord.HTTPException as e:  # 메시지가 삭제된 경우 등
            print(f"⚠️ 투표 메시지 수정 실패 (message={message_id}): {e}")
        try:
            await channel.send(result_text(poll), allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            print(f"⚠️ 투표 결과 발표 실패 (channel={poll.channel_id}): {e}")
        return True

    async def handle_manual_close(self, interaction, message_id_text: str):
        text = (message_id_text or "").strip()
        if not text.isdigit():
            await interaction.response.send_message("❌ 메시지 ID는 숫자로 입력해 주세요.", ephemeral=True)
            return False
        poll = self.store.get_poll(int(text))
        # 다른 서버의 투표는 ID를 알아도 존재 여부조차 알 수 없도록 같은 안내
        if poll is None or poll.guild_id != interaction.guild_id:
            await interaction.response.send_message("❌ 해당 ID의 투표를 찾을 수 없습니다.", ephemeral=True)
            return False
        if interaction.user.id != poll.creator_id and not _is_admin(interaction.user):
            await interaction.response.send_message("❌ 투표를 만든 사람이나 관리자만 마감할 수 있습니다.", ephemeral=True)
            return False
        if poll.closed:
            await interaction.response.send_message("ℹ️ 이미 마감된 투표입니다.", ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True)
        closed = await self.close_poll(poll.message_id)
        await interaction.followup.send(
            "✅ 투표를 마감했습니다." if closed else "ℹ️ 이미 마감된 투표입니다.", ephemeral=True
        )
        return closed

    # ── 재시작 복원 ───────────────────────────────────────────────────────
    async def restore(self) -> int:
        if not self.template_view_added:
            self.bot.add_view(PollView(self))
            self.template_view_added = True
        count = 0
        for poll in self.store.open_polls():
            if poll.closes_at is not None:
                self.schedule_close(poll)
            count += 1
        return count


# ─────────────────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────────────────

def register(bot, db_path: Optional[str] = None) -> PollManager:
    """/투표, /투표마감 명령을 bot.tree 에 등록하고 PollManager 를 bot.poll_manager 로 둔다."""
    manager = PollManager(bot, db_path)
    bot.poll_manager = manager

    @bot.tree.command(name="투표", description="버튼으로 참여하는 투표를 만듭니다.")
    @app_commands.guild_only()
    @app_commands.describe(
        질문="투표 질문",
        항목="쉼표(,)로 구분한 2~10개 항목. 예: 짜장, 짬뽕, 볶음밥",
        마감="(선택) 자동 마감 시각. 예: '30분 뒤', '오후 6시', '내일 오전 9시'",
        중복허용="여러 항목에 투표할 수 있게 할지 (기본: 아니오)",
        익명="투표자를 숨길지 (기본: 예)",
    )
    async def create_poll(interaction: discord.Interaction, 질문: str, 항목: str,
                          마감: Optional[str] = None, 중복허용: bool = False, 익명: bool = True):
        await manager.handle_create(interaction, 질문, 항목, 마감, 중복허용, 익명)

    @bot.tree.command(name="투표마감", description="진행 중인 투표를 마감합니다. (만든 사람/관리자)")
    @app_commands.guild_only()
    @app_commands.describe(메시지id="마감할 투표 메시지의 ID (메시지 우클릭 → ID 복사)")
    async def close_poll_command(interaction: discord.Interaction, 메시지id: str):
        await manager.handle_manual_close(interaction, 메시지id)

    return manager


async def restore(bot) -> int:
    """setup_hook 에서 호출: persistent view 등록 및 자동 마감 태스크 복원."""
    manager = getattr(bot, "poll_manager", None) or register(bot)
    restored = await manager.restore()
    print(f"🗳️ 진행 중인 투표 {restored}개를 복원했습니다.")
    return restored
