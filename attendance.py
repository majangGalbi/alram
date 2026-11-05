"""출석체크 기능 (/출석, /출석현황, /랭킹).

bot.py 에서는 ``attendance.register(bot)`` 한 줄로만 연결한다.
데이터는 SQLite 에 저장하며 경로는 환경변수 ATTENDANCE_DB_PATH (기본 attendance.db).
날짜 경계는 Asia/Seoul 자정 기준이다.
"""
import asyncio
import calendar
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

import discord
import pytz
from discord import app_commands

SEOUL = pytz.timezone("Asia/Seoul")
DB_ENV = "ATTENDANCE_DB_PATH"
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")  # 실행 위치(cwd)와 무관하게 bot.py 옆에 저장

BASE_POINTS = 10            # 출석 기본 포인트
STREAK_BONUS_PER_DAY = 2    # 연속 2일째부터 하루당 추가 포인트
MAX_STREAK_BONUS = 20       # 연속 보너스 상한
WEEKLY_BONUS = 50           # 연속 7일·14일·21일… 마다 추가 보너스
WEEKLY_INTERVAL = 7
RANKING_LIMIT = 10

# 사용자 표시용 / 내부 컬럼
RANK_KEYS = {"포인트": "points", "연속": "streak", "누적": "total"}

NO_PING = discord.AllowedMentions.none()


def today_kst(now: Optional[datetime] = None) -> date:
    """Asia/Seoul 기준 오늘 날짜. naive datetime 은 UTC 로 간주한다."""
    if now is None:
        now = datetime.now(pytz.utc)
    elif now.tzinfo is None:
        now = pytz.utc.localize(now)
    return now.astimezone(SEOUL).date()


def calc_points(streak: int) -> Dict[str, int]:
    """연속 출석 일수에 따른 포인트 내역."""
    if streak < 1:
        raise ValueError("streak 은 1 이상이어야 합니다.")
    streak_bonus = min((streak - 1) * STREAK_BONUS_PER_DAY, MAX_STREAK_BONUS)
    weekly = WEEKLY_BONUS if streak % WEEKLY_INTERVAL == 0 else 0
    return {"base": BASE_POINTS, "streak_bonus": streak_bonus, "weekly_bonus": weekly,
            "total": BASE_POINTS + streak_bonus + weekly}


def next_streak(last_date: Optional[str], today: date, prev_streak: int) -> int:
    """마지막 출석일이 어제면 이어가고, 아니면 1부터 다시 시작."""
    if last_date and date.fromisoformat(last_date) == today - timedelta(days=1):
        return prev_streak + 1
    return 1


def effective_streak(last_date: Optional[str], streak: int, today: date) -> int:
    """오늘/어제 출석했으면 연속 유지, 그보다 오래됐으면 끊긴 것(0)."""
    if not last_date:
        return 0
    d = date.fromisoformat(last_date)
    return streak if d >= today - timedelta(days=1) else 0


@dataclass
class CheckInResult:
    already: bool
    date: str
    streak: int
    total: int
    best_streak: int
    points: int                # 누적 포인트
    awarded: int = 0           # 이번에 받은 포인트
    breakdown: Optional[Dict[str, int]] = None


class AttendanceStore:
    """SQLite 저장소. 호출마다 연결을 새로 열어 스레드 간 공유 문제를 피한다."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv(DB_ENV) or DEFAULT_DB_PATH
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if not self._initialized:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkins (
                    guild_id INTEGER NOT NULL,
                    user_id  INTEGER NOT NULL,
                    day      TEXT    NOT NULL,
                    points   INTEGER NOT NULL,
                    streak   INTEGER NOT NULL,
                    UNIQUE (guild_id, user_id, day)
                );
                CREATE TABLE IF NOT EXISTS stats (
                    guild_id    INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    total       INTEGER NOT NULL DEFAULT 0,
                    streak      INTEGER NOT NULL DEFAULT 0,
                    best_streak INTEGER NOT NULL DEFAULT 0,
                    points      INTEGER NOT NULL DEFAULT 0,
                    last_date   TEXT,
                    PRIMARY KEY (guild_id, user_id)
                );
                """
            )
            self._initialized = True
        return conn

    def check_in(self, guild_id: int, user_id: int, now: Optional[datetime] = None) -> CheckInResult:
        today = today_kst(now)
        day = today.isoformat()
        conn = self._connect()
        try:
            # BEGIN IMMEDIATE: 쓰기 잠금을 먼저 잡아 동시 요청이 같은 이전 상태를 읽지 않게 함
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM stats WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                ).fetchone()
                prev_streak = row["streak"] if row else 0
                last_date = row["last_date"] if row else None
                streak = next_streak(last_date, today, prev_streak)
                breakdown = calc_points(streak)
                try:
                    conn.execute(
                        "INSERT INTO checkins (guild_id, user_id, day, points, streak) VALUES (?,?,?,?,?)",
                        (guild_id, user_id, day, breakdown["total"], streak),
                    )
                except sqlite3.IntegrityError:
                    # UNIQUE(guild_id, user_id, day) 위반 = 오늘 이미 출석
                    conn.execute("ROLLBACK")
                    return self._already(guild_id, user_id, day)
                total = (row["total"] if row else 0) + 1
                best = max(row["best_streak"] if row else 0, streak)
                points = (row["points"] if row else 0) + breakdown["total"]
                conn.execute(
                    """INSERT INTO stats (guild_id, user_id, total, streak, best_streak, points, last_date)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(guild_id, user_id) DO UPDATE SET
                         total=excluded.total, streak=excluded.streak, best_streak=excluded.best_streak,
                         points=excluded.points, last_date=excluded.last_date""",
                    (guild_id, user_id, total, streak, best, points, day),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return CheckInResult(False, day, streak, total, best, points, breakdown["total"], breakdown)
        finally:
            conn.close()

    def _already(self, guild_id: int, user_id: int, day: str) -> CheckInResult:
        s = self.get_stats(guild_id, user_id) or {}
        return CheckInResult(True, day, s.get("streak", 0), s.get("total", 0),
                             s.get("best_streak", 0), s.get("points", 0))

    def get_stats(self, guild_id: int, user_id: int) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM stats WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def month_days(self, guild_id: int, user_id: int, year: int, month: int) -> Set[int]:
        prefix = f"{year:04d}-{month:02d}-"
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT day FROM checkins WHERE guild_id=? AND user_id=? AND day LIKE ?",
                (guild_id, user_id, prefix + "%"),
            ).fetchall()
            return {int(r["day"][8:10]) for r in rows}
        finally:
            conn.close()

    def ranking(self, guild_id: int, key: str = "points", limit: int = RANKING_LIMIT,
                now: Optional[datetime] = None) -> List[dict]:
        if key not in RANK_KEYS.values():
            raise ValueError(f"알 수 없는 랭킹 기준: {key}")
        yesterday = (today_kst(now) - timedelta(days=1)).isoformat()
        # 연속 기준은 끊긴 스트릭을 0 으로 취급
        eff = "CASE WHEN last_date >= :y THEN streak ELSE 0 END"
        order_col = {"points": "points", "total": "total", "streak": eff}[key]
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""SELECT user_id, total, best_streak, points, {eff} AS streak
                    FROM stats WHERE guild_id=:g AND ({order_col}) > 0
                    ORDER BY {order_col} DESC, points DESC, total DESC, user_id ASC
                    LIMIT :n""",
                {"g": guild_id, "y": yesterday, "n": limit},
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def render_calendar(year: int, month: int, days: Set[int], today: Optional[date] = None) -> str:
    """월요일 시작 텍스트 달력. 출석일은 ✅(이모지 폭 때문에 2칸 고정) 표시."""
    lines = [f"{year}년 {month}월", "월 화 수 목 금 토 일"]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        cells = []
        for d in week:
            if d == 0:
                cells.append("  ")
            elif d in days:
                cells.append("✅")
            else:
                cells.append(f"{d:2d}")
        lines.append(" ".join(cells).rstrip())
    return "```\n" + "\n".join(lines) + "\n```"


# ───────────────────── 명령 처리 (테스트에서 직접 호출) ─────────────────────

async def _send_error(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def _name(user) -> str:
    return discord.utils.escape_markdown(getattr(user, "display_name", None) or str(user))


async def handle_checkin(interaction: discord.Interaction, store: AttendanceStore,
                         now: Optional[datetime] = None) -> Optional[CheckInResult]:
    if interaction.guild_id is None:
        await _send_error(interaction, "❌ 출석체크는 서버에서만 사용할 수 있습니다.")
        return None
    result = await asyncio.to_thread(store.check_in, interaction.guild_id, interaction.user.id, now)
    if result.already:
        await interaction.response.send_message(
            f"📌 오늘({result.date}) 이미 출석했습니다. 현재 연속 {result.streak}일 · "
            f"누적 {result.total}일 · {result.points}P\n자정(한국시간) 이후 다시 출석할 수 있어요.",
            ephemeral=True,
        )
        return result
    b = result.breakdown
    detail = f"기본 {b['base']}"
    if b["streak_bonus"]:
        detail += f" + 연속 보너스 {b['streak_bonus']}"
    if b["weekly_bonus"]:
        detail += f" + {WEEKLY_INTERVAL}일 보너스 {b['weekly_bonus']} 🎉"
    await interaction.response.send_message(
        f"✅ {_name(interaction.user)} 님 출석 완료! (+{result.awarded}P: {detail})\n"
        f"🔥 연속 {result.streak}일 · 📅 누적 {result.total}일 · 💰 총 {result.points}P",
        allowed_mentions=NO_PING,
    )
    return result


async def handle_status(interaction: discord.Interaction, store: AttendanceStore,
                        user=None, now: Optional[datetime] = None) -> None:
    if interaction.guild_id is None:
        await _send_error(interaction, "❌ 출석현황은 서버에서만 사용할 수 있습니다.")
        return
    target = user or interaction.user
    today = today_kst(now)
    stats = await asyncio.to_thread(store.get_stats, interaction.guild_id, target.id)
    days = await asyncio.to_thread(store.month_days, interaction.guild_id, target.id, today.year, today.month)
    stats = stats or {"total": 0, "streak": 0, "best_streak": 0, "points": 0, "last_date": None}
    streak = effective_streak(stats["last_date"], stats["streak"], today)
    checked_today = stats["last_date"] == today.isoformat()
    embed = discord.Embed(title=f"📋 {_name(target)} 님의 출석현황", color=discord.Color.green())
    embed.add_field(name="누적 출석", value=f"{stats['total']}일")
    embed.add_field(name="연속 출석", value=f"{streak}일")
    embed.add_field(name="최장 연속", value=f"{stats['best_streak']}일")
    embed.add_field(name="포인트", value=f"{stats['points']}P")
    embed.add_field(name="오늘 출석", value="완료 ✅" if checked_today else "아직 ❌")
    embed.add_field(name=f"이번 달 ({len(days)}일)", value=render_calendar(today.year, today.month, days, today),
                    inline=False)
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_PING)


async def handle_ranking(interaction: discord.Interaction, store: AttendanceStore,
                         기준: str = "포인트", now: Optional[datetime] = None) -> None:
    if interaction.guild_id is None:
        await _send_error(interaction, "❌ 랭킹은 서버에서만 사용할 수 있습니다.")
        return
    key = RANK_KEYS.get(기준)
    if key is None:
        await _send_error(interaction, "❌ 기준은 포인트 / 연속 / 누적 중 하나여야 합니다.")
        return
    rows = await asyncio.to_thread(store.ranking, interaction.guild_id, key, RANKING_LIMIT, now)
    embed = discord.Embed(title=f"🏆 출석 랭킹 ({기준})", color=discord.Color.gold())
    if not rows:
        embed.description = "아직 출석 기록이 없습니다. `/출석` 으로 첫 출석을 해 보세요!"
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        unit = {"points": "P", "streak": "일 연속", "total": "일"}[key]
        lines = []
        for i, r in enumerate(rows, 1):
            member = interaction.guild.get_member(r["user_id"]) if interaction.guild else None
            who = _name(member) if member else f"<@{r['user_id']}>"
            lines.append(f"{medals.get(i, f'`{i:>2}.`')} {who} — **{r[key]}{unit}**")
        embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_PING)


def register(bot, store: Optional[AttendanceStore] = None) -> AttendanceStore:
    """봇의 명령 트리에 출석 관련 명령을 등록한다. (DB 파일은 첫 사용 시 생성)"""
    store = store or AttendanceStore()

    @app_commands.command(name="출석", description="오늘 출석체크를 하고 포인트를 받습니다. (한국시간 자정 초기화)")
    @app_commands.guild_only()
    async def checkin_cmd(interaction: discord.Interaction):
        await handle_checkin(interaction, store)

    @app_commands.command(name="출석현황", description="출석 누적/연속 기록과 이번 달 출석 달력을 보여줍니다.")
    @app_commands.describe(사용자="조회할 사용자 (비우면 본인)")
    @app_commands.guild_only()
    async def status_cmd(interaction: discord.Interaction, 사용자: Optional[discord.Member] = None):
        await handle_status(interaction, store, 사용자)

    @app_commands.command(name="랭킹", description="서버 출석 랭킹 상위 10명을 보여줍니다.")
    @app_commands.describe(기준="정렬 기준 (기본: 포인트)")
    @app_commands.choices(기준=[app_commands.Choice(name=k, value=k) for k in RANK_KEYS])
    @app_commands.guild_only()
    async def ranking_cmd(interaction: discord.Interaction, 기준: Optional[app_commands.Choice[str]] = None):
        await handle_ranking(interaction, store, 기준.value if 기준 else "포인트")

    for cmd in (checkin_cmd, status_cmd, ranking_cmd):
        bot.tree.add_command(cmd)
    bot.attendance_store = store
    return store
