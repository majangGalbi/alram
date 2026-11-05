"""서버 달력 일정표 기능.

슬래시 명령
-----------
- ``/일정추가 제목 일시 [종료] [설명] [종일] [알림분]``
- ``/일정목록 [기간: 오늘|이번주|이번달|다음달]``
- ``/달력 [년] [월] [이미지]``
- ``/일정수정 번호 [제목] [일시] [종료] [설명] [종일] [알림분]``
- ``/일정삭제 번호``

설계
----
- 표준 라이브러리 ``calendar`` 와 이름이 겹치지 않도록 모듈명은 ``schedule_calendar``.
- 공개 진입점은 ``register(bot)`` 하나. bot.py 는 import + 호출 한 줄만 추가한다.
- 데이터는 SQLite(``CALENDAR_DB_PATH`` 환경변수, 기본 ``calendar.db``), 모든 조회·수정은
  ``guild_id`` 조건을 붙여 서버별로 격리한다.
- 시간은 모두 한국 시간(Asia/Seoul) 기준 naive datetime 으로 저장한다.
- 알림은 APScheduler 를 쓰지 않고 이 모듈 안의 asyncio 백그라운드 루프(1분 주기)가
  DB 를 폴링한다. 발송 전에 ``reminded`` 플래그를 원자적으로 0→1 로 바꿔(UPDATE ...
  WHERE reminded=0) 루프가 겹치거나 재시작돼도 중복 발송하지 않는다.
"""

from __future__ import annotations

import asyncio
import calendar as _stdcal
import io
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Dict, Iterable, List, Literal, Optional, Tuple

import discord
import pytz
from discord import app_commands

from time_parser import TimeParseError, parse_time

__all__ = ["register"]

SEOUL = pytz.timezone("Asia/Seoul")
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar.db")  # 실행 위치(cwd)와 무관하게 bot.py 옆에 저장
DEFAULT_REMIND_MINUTES = 10
MAX_REMIND_MINUTES = 7 * 24 * 60          # 최대 1주일 전
ALL_DAY_REMIND_TIME = time(9, 0)          # 종일 일정은 당일 오전 9시 기준
POLL_INTERVAL_SECONDS = 60
REMIND_GRACE = timedelta(minutes=10)      # 봇이 잠시 꺼져 있었어도 10분 이내면 발송

TITLE_MAX = 100
DESCRIPTION_MAX = 1000
# 디스코드 임베드 한도
EMBED_TITLE_LIMIT = 256
EMBED_DESC_LIMIT = 4096
EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_MAX_FIELDS = 25
EMBED_TOTAL_LIMIT = 6000
MESSAGE_LIMIT = 2000

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]  # date.weekday() 순서
PERIODS = ("오늘", "이번주", "이번달", "다음달")

NO_MENTIONS = discord.AllowedMentions.none()

FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "NanumGothic-Regular.ttf"),  # 저장소 포함(OFL)
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgun.ttf",
]


# ─────────────────────────────────────────────────────────────────────────────
# 시간 유틸
# ─────────────────────────────────────────────────────────────────────────────
def now_seoul() -> datetime:
    """현재 한국 시간 (naive, 초 단위)."""
    return datetime.now(SEOUL).replace(tzinfo=None, microsecond=0)


def parse_date_only(text: str, base_time: datetime) -> date:
    """날짜만 있는 표현('내일', '12월 25일', '다음주 금요일', '2026-12-25')을 해석한다.

    time_parser.parse_time 은 날짜만 입력하면 거부하므로, 시각(정오)을 덧붙여 재사용한다.
    입력에 이미 시각이 들어 있으면 종일 일정에 맞지 않으므로 오류로 처리한다.
    """
    if text is None or not str(text).strip():
        raise TimeParseError("날짜를 입력해 주세요. 예: '내일', '12월 25일', '다음주 금요일'")
    raw = str(text).strip()
    try:
        parse_time(raw, base_time)
    except TimeParseError:
        pass  # 날짜만 입력한 경우 여기로 온다 (정상 경로)
    else:
        raise TimeParseError("종일 일정은 날짜만 입력해 주세요. 예: '내일', '12월 25일', '다음주 금요일'")
    try:
        return parse_time(f"{raw} 정오", base_time).date()
    except TimeParseError:
        raise TimeParseError(
            f"'{raw[:50]}' 을(를) 날짜로 이해하지 못했어요. 예: '내일', '모레', '12월 25일', '2026-12-25', '다음주 금요일'"
        ) from None


_DATE_HINT_RE = re.compile(
    r"오늘|금일|내일|낼|명일|모레|글피|어제|요일|주|달|월|년|\d+\s*일|\d+\s*[/.-]\s*\d+|"
    r"today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|week",
    re.IGNORECASE,
)


def parse_end_time(text: str, start: datetime, now: datetime) -> datetime:
    """종료 일시 해석. 날짜가 들어 있으면('내일 오후 1시') 현재 기준, 시각·기간만
    있으면('오후 5시', '2시간 뒤') 시작 시각 기준으로 해석한다."""
    base = now if _DATE_HINT_RE.search(str(text or "")) else start
    return parse_time(text, base)


def period_range(period: str, today: date) -> Tuple[date, date]:
    """기간 이름 → [시작일, 종료일) (종료일은 포함하지 않음). 주는 일요일 시작(달력과 동일)."""
    if period == "오늘":
        return today, today + timedelta(days=1)
    if period == "이번주":
        start = today - timedelta(days=(today.weekday() + 1) % 7)  # 일요일
        return start, start + timedelta(days=7)
    if period == "이번달":
        return month_range(today.year, today.month)
    if period == "다음달":
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return month_range(y, m)
    raise ValueError(f"알 수 없는 기간: {period}")


def month_range(year: int, month: int) -> Tuple[date, date]:
    start = date(year, month, 1)
    days = _stdcal.monthrange(year, month)[1]
    return start, start + timedelta(days=days)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def clean_text(text: Optional[str], limit: int) -> str:
    """앞뒤 공백 제거 + 길이 제한. 코드블록/임베드를 깨지 않도록 백틱 3개는 치환."""
    if text is None:
        return ""
    return truncate(str(text).strip().replace("```", "'''"), limit)


# ─────────────────────────────────────────────────────────────────────────────
# 데이터 모델 / 저장소
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Event:
    id: int
    guild_id: int
    channel_id: int
    creator_id: int
    title: str
    description: str
    start: datetime
    end: Optional[datetime]
    all_day: bool
    remind_minutes: Optional[int]
    reminded: bool

    @property
    def last_day(self) -> date:
        """일정이 걸쳐 있는 마지막 날짜 (포함)."""
        if self.end is None:
            return self.start.date()
        if not self.all_day and self.end.time() == time(0, 0) and self.end.date() > self.start.date():
            return self.end.date() - timedelta(days=1)  # 자정에 끝나면 전날까지
        return max(self.end.date(), self.start.date())

    def days(self) -> Iterable[date]:
        d = self.start.date()
        while d <= self.last_day:
            yield d
            d += timedelta(days=1)

    def overlaps(self, start: date, end_exclusive: date) -> bool:
        return self.start.date() < end_exclusive and self.last_day >= start


def compute_remind_at(start: datetime, all_day: bool, remind_minutes: Optional[int]) -> Optional[datetime]:
    if remind_minutes is None or remind_minutes < 0:
        return None
    if all_day:
        return datetime.combine(start.date(), ALL_DAY_REMIND_TIME) - timedelta(minutes=remind_minutes)
    return start - timedelta(minutes=remind_minutes)


def _fmt_ts(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else None


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


class CalendarStore:
    """SQLite 저장소. 모든 공개 메서드는 guild_id 범위 안에서만 동작한다."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("CALENDAR_DB_PATH") or DEFAULT_DB_PATH
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                start_ts TEXT NOT NULL,
                end_ts TEXT,
                all_day INTEGER NOT NULL DEFAULT 0,
                remind_minutes INTEGER,
                remind_at TEXT,
                reminded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_guild_start ON events (guild_id, start_ts)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_remind ON events (reminded, remind_at)")
        self.conn.commit()

    def close(self):
        self.conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> Event:
        return Event(
            id=r["id"], guild_id=r["guild_id"], channel_id=r["channel_id"], creator_id=r["creator_id"],
            title=r["title"], description=r["description"] or "",
            start=_parse_ts(r["start_ts"]), end=_parse_ts(r["end_ts"]), all_day=bool(r["all_day"]),
            remind_minutes=r["remind_minutes"], reminded=bool(r["reminded"]),
        )

    def add(self, guild_id: int, channel_id: int, creator_id: int, title: str, start: datetime,
            end: Optional[datetime] = None, description: str = "", all_day: bool = False,
            remind_minutes: Optional[int] = DEFAULT_REMIND_MINUTES, now: Optional[datetime] = None) -> Event:
        remind_at = compute_remind_at(start, all_day, remind_minutes)
        cur = self.conn.execute(
            """INSERT INTO events (guild_id, channel_id, creator_id, title, description, start_ts, end_ts,
                                   all_day, remind_minutes, remind_at, reminded, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (guild_id, channel_id, creator_id, title, description or "", _fmt_ts(start), _fmt_ts(end),
             int(all_day), remind_minutes, _fmt_ts(remind_at), _fmt_ts(now or now_seoul())),
        )
        self.conn.commit()
        return self.get(guild_id, cur.lastrowid)

    def get(self, guild_id: int, event_id: int) -> Optional[Event]:
        r = self.conn.execute("SELECT * FROM events WHERE guild_id = ? AND id = ?", (guild_id, event_id)).fetchone()
        return self._row(r) if r else None

    def list_range(self, guild_id: int, start: date, end_exclusive: date) -> List[Event]:
        """[start, end_exclusive) 기간과 겹치는 일정 (시작 시각 순)."""
        start_s = _fmt_ts(datetime.combine(start, time.min))
        end_s = _fmt_ts(datetime.combine(end_exclusive, time.min))
        rows = self.conn.execute(
            """SELECT * FROM events WHERE guild_id = ? AND start_ts < ?
                 AND COALESCE(end_ts, start_ts) >= ? ORDER BY start_ts, id""",
            (guild_id, end_s, start_s),
        ).fetchall()
        return [e for e in map(self._row, rows) if e.overlaps(start, end_exclusive)]

    def update(self, guild_id: int, event_id: int, **fields) -> Optional[Event]:
        ev = self.get(guild_id, event_id)
        if ev is None:
            return None
        for k, v in fields.items():
            if not hasattr(ev, k) or k in ("id", "guild_id", "creator_id", "reminded"):
                raise ValueError(f"수정할 수 없는 필드: {k}")
            setattr(ev, k, v)
        remind_at = compute_remind_at(ev.start, ev.all_day, ev.remind_minutes)
        # 시간/알림 설정이 바뀌면 알림을 다시 보낼 수 있도록 플래그 초기화
        reset = any(k in fields for k in ("start", "all_day", "remind_minutes"))
        self.conn.execute(
            """UPDATE events SET channel_id=?, title=?, description=?, start_ts=?, end_ts=?, all_day=?,
                   remind_minutes=?, remind_at=?, reminded=CASE WHEN ? THEN 0 ELSE reminded END
               WHERE guild_id=? AND id=?""",
            (ev.channel_id, ev.title, ev.description, _fmt_ts(ev.start), _fmt_ts(ev.end), int(ev.all_day),
             ev.remind_minutes, _fmt_ts(remind_at), int(reset), guild_id, event_id),
        )
        self.conn.commit()
        return self.get(guild_id, event_id)

    def delete(self, guild_id: int, event_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM events WHERE guild_id = ? AND id = ?", (guild_id, event_id))
        self.conn.commit()
        return cur.rowcount == 1

    def exists(self, guild_id: int, title: str, start: datetime) -> bool:
        r = self.conn.execute("SELECT 1 FROM events WHERE guild_id = ? AND title = ? AND start_ts = ? LIMIT 1",
                              (guild_id, title, _fmt_ts(start))).fetchone()
        return r is not None

    def bulk_add(self, guild_id: int, channel_id: int, creator_id: int, items: List["BulkItem"],
                 now: Optional[datetime] = None) -> Tuple[int, int]:
        """한 트랜잭션으로 일괄 INSERT. 같은 제목+같은 시작 일시(기존 또는 배치 안)는 건너뜀.

        :returns: (추가 수, 중복으로 건너뛴 수)
        """
        created = _fmt_ts(now or now_seoul())
        added = skipped = 0
        seen = set()
        with self.conn:  # 하나의 트랜잭션: 예외 시 전체 롤백
            for it in items:
                key = (it.title, _fmt_ts(it.start))
                if key in seen or self.exists(guild_id, it.title, it.start):
                    skipped += 1
                    continue
                seen.add(key)
                self.conn.execute(
                    """INSERT INTO events (guild_id, channel_id, creator_id, title, description, start_ts, end_ts,
                                           all_day, remind_minutes, remind_at, reminded, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, ?)""",
                    (guild_id, channel_id, creator_id, it.title, it.description, _fmt_ts(it.start),
                     int(it.all_day), it.remind_minutes,
                     _fmt_ts(compute_remind_at(it.start, it.all_day, it.remind_minutes)), created),
                )
                added += 1
        return added, skipped

    def due_reminders(self, now: datetime) -> List[Event]:
        """알림 시각이 되었고 아직 발송하지 않은 일정 (모든 서버)."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE reminded = 0 AND remind_at IS NOT NULL AND remind_at <= ? ORDER BY remind_at, id",
            (_fmt_ts(now),),
        ).fetchall()
        return [self._row(r) for r in rows]

    def remind_at(self, event_id: int) -> Optional[datetime]:
        r = self.conn.execute("SELECT remind_at FROM events WHERE id = ?", (event_id,)).fetchone()
        return _parse_ts(r["remind_at"]) if r else None

    def claim_reminder(self, event_id: int) -> bool:
        """reminded 0→1 원자적 전환. 이미 처리됐으면 False (중복 발송 방지)."""
        cur = self.conn.execute("UPDATE events SET reminded = 1 WHERE id = ? AND reminded = 0", (event_id,))
        self.conn.commit()
        return cur.rowcount == 1


# ─────────────────────────────────────────────────────────────────────────────
# 표시 / 렌더링
# ─────────────────────────────────────────────────────────────────────────────
def format_date_ko(d: date) -> str:
    return f"{d.year}년 {d.month}월 {d.day}일 ({WEEKDAY_KO[d.weekday()]})"


def format_clock(dt: datetime) -> str:
    ampm = "오전" if dt.hour < 12 else "오후"
    h = dt.hour % 12 or 12
    return f"{ampm} {h}:{dt.minute:02d}"


def format_when(ev: Event) -> str:
    if ev.all_day:
        s = format_date_ko(ev.start.date())
        if ev.end is not None and ev.end.date() > ev.start.date():
            s += f" ~ {format_date_ko(ev.end.date())}"
        return s + " (종일)"
    s = f"{format_date_ko(ev.start.date())} {format_clock(ev.start)}"
    if ev.end is not None:
        if ev.end.date() == ev.start.date():
            s += f" ~ {format_clock(ev.end)}"
        else:
            s += f" ~ {format_date_ko(ev.end.date())} {format_clock(ev.end)}"
    return s


def event_line(ev: Event, day: Optional[date] = None) -> str:
    if ev.all_day or (day is not None and day != ev.start.date()):
        when = "종일" if ev.all_day else "(계속)"
    else:
        when = format_clock(ev.start)
    return f"`#{ev.id}` {when} · {discord.utils.escape_markdown(ev.title)}"


def group_by_day(events: Iterable[Event], start: date, end_exclusive: date) -> Dict[date, List[Event]]:
    out: Dict[date, List[Event]] = {}
    for ev in events:
        for d in ev.days():
            if start <= d < end_exclusive:
                out.setdefault(d, []).append(ev)
    return dict(sorted(out.items()))


def build_list_embed(title: str, events: List[Event], start: date, end_exclusive: date) -> discord.Embed:
    """날짜별 필드로 묶은 임베드. 필드 25개·값 1024자·총 6000자 한도를 지킨다."""
    embed = discord.Embed(title=truncate(title, EMBED_TITLE_LIMIT), color=0x5865F2)
    grouped = group_by_day(events, start, end_exclusive)
    if not grouped:
        embed.description = "등록된 일정이 없습니다. `/일정추가` 로 추가해 보세요."
        return embed
    total = len(embed.title)
    footer_reserve = 100
    shown_days = 0
    for d, evs in grouped.items():
        if shown_days >= EMBED_MAX_FIELDS:
            break
        name = format_date_ko(d)
        lines, used = [], 0
        for i, ev in enumerate(evs):
            line = truncate(event_line(ev, d), 200)
            rest = len(evs) - i
            if used + len(line) + 1 > EMBED_FIELD_VALUE_LIMIT - 20:
                lines.append(f"… 외 {rest}건")
                break
            lines.append(line)
            used += len(line) + 1
        value = "\n".join(lines)
        if total + len(name) + len(value) > EMBED_TOTAL_LIMIT - footer_reserve:
            break
        embed.add_field(name=name, value=value, inline=False)
        total += len(name) + len(value)
        shown_days += 1
    hidden = len(grouped) - shown_days
    footer = f"총 {len(events)}건"
    if hidden > 0:
        footer += f" · 표시 한도로 {hidden}일치 생략"
    embed.set_footer(text=footer)
    return embed


def render_month_text(year: int, month: int, event_days: Iterable[date], today: Optional[date] = None) -> str:
    """월간 달력(일~토)을 코드블록용 텍스트로. 오늘은 [ ], 일정 있는 날은 * 표시.

    칸은 5칸 고정폭: 앞(1) + 날짜(2) + 뒤(1) + 일정표시(1). 한글 요일은 2칸 폭으로 계산.
    """
    marked = {d for d in event_days if d.year == year and d.month == month}
    cal = _stdcal.Calendar(firstweekday=6)  # 일요일 시작
    lines = [f"{year}년 {month}월", "".join(f" {w}  " for w in "일월화수목금토").rstrip()]
    for week in cal.monthdayscalendar(year, month):
        cells = []
        for day in week:
            if day == 0:
                cells.append("     ")
                continue
            d = date(year, month, day)
            is_today = today == d
            cells.append(("[" if is_today else " ") + f"{day:>2}" + ("]" if is_today else " ")
                         + ("*" if d in marked else " "))
        lines.append("".join(cells).rstrip())
    lines.append("")
    lines.append("[n] 오늘   * 일정 있음")
    return "\n".join(lines)


def find_korean_font() -> Optional[str]:
    env = os.getenv("CALENDAR_FONT_PATH")
    candidates = ([env] if env else []) + FONT_CANDIDATES
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


# PNG 달력 레이아웃 (픽셀)
PNG_PAD = 24
PNG_CELL_W = 180
PNG_CELL_H = 132
PNG_TITLE_H = 70
PNG_HEAD_H = 34
PNG_LINE_H = 19
PNG_DAY_H = 30
PNG_MAX_LINES = (PNG_CELL_H - PNG_DAY_H - 6) // PNG_LINE_H   # 칸 하나에 들어가는 일정 줄 수 (=5)

COLOR_BG = (255, 255, 255)
COLOR_GRID = (205, 208, 214)
COLOR_TEXT = (40, 40, 40)
COLOR_SUN = (214, 48, 49)
COLOR_SAT = (41, 98, 214)
COLOR_TODAY_BG = (255, 243, 191)
COLOR_TODAY_RING = (240, 170, 0)
COLOR_EVENT = (52, 64, 120)
COLOR_MORE = (120, 120, 120)
COLOR_OTHER_MONTH = (246, 247, 249)


def png_size(year: int, month: int) -> Tuple[int, int]:
    weeks = len(_stdcal.Calendar(firstweekday=6).monthdayscalendar(year, month))
    return (PNG_PAD * 2 + PNG_CELL_W * 7,
            PNG_PAD + PNG_TITLE_H + PNG_HEAD_H + PNG_CELL_H * weeks + PNG_PAD)


def cell_labels(evs: List[Event], day: date, max_lines: int = PNG_MAX_LINES) -> List[str]:
    """한 날짜 칸에 넣을 일정 문자열들. 넘치면 마지막 줄을 '+N개' 로."""
    labels = []
    for ev in evs:
        if ev.all_day:
            prefix = "종일"
        elif day != ev.start.date():
            prefix = "계속"
        else:
            prefix = f"{ev.start.hour:02d}:{ev.start.minute:02d}"
        labels.append(f"{prefix} {ev.title}")
    if len(labels) > max_lines:
        keep = max_lines - 1
        labels = labels[:keep] + [f"+{len(evs) - keep}개"]
    return labels


def _fit(draw, text: str, font, max_w: int) -> str:
    """픽셀 폭에 맞게 말줄임."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def render_month_png(year: int, month: int, events: List[Event], today: Optional[date] = None,
                     font_path: Optional[str] = None) -> Optional[bytes]:
    """Pillow 로 실제 달력 같은 월간 격자 PNG 를 그린다(일~토 7열, 칸 안에 일정 제목·시각).

    Pillow 나 한글 폰트가 없으면 None 을 돌려 호출부가 텍스트 달력으로 폴백한다.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    font_path = font_path or find_korean_font()
    if not font_path:
        return None
    bold_path = font_path.replace("-Regular", "-Bold")
    if bold_path == font_path or not os.path.isfile(bold_path):
        bold_path = font_path
    try:
        f_title = ImageFont.truetype(bold_path, 34)
        f_head = ImageFont.truetype(bold_path, 18)
        f_day = ImageFont.truetype(bold_path, 18)
        f_ev = ImageFont.truetype(font_path, 14)
    except OSError:
        return None

    start, end = month_range(year, month)
    grouped = group_by_day(events, start, end)
    weeks = _stdcal.Calendar(firstweekday=6).monthdayscalendar(year, month)
    width, height = png_size(year, month)
    img = Image.new("RGB", (width, height), COLOR_BG)
    dr = ImageDraw.Draw(img)

    dr.text((PNG_PAD, PNG_PAD), f"{year}년 {month}월", font=f_title, fill=COLOR_TEXT)
    total = sum(len(v) for v in grouped.values())
    sub = f"일정 {len(events)}건" if total else "등록된 일정 없음"
    dr.text((width - PNG_PAD - dr.textlength(sub, font=f_head), PNG_PAD + 14), sub, font=f_head, fill=COLOR_MORE)

    head_y = PNG_PAD + PNG_TITLE_H
    for i, w in enumerate("일월화수목금토"):
        color = COLOR_SUN if i == 0 else COLOR_SAT if i == 6 else COLOR_TEXT
        x = PNG_PAD + i * PNG_CELL_W + PNG_CELL_W // 2 - dr.textlength(w, font=f_head) / 2
        dr.text((x, head_y + 6), w, font=f_head, fill=color)

    grid_y = head_y + PNG_HEAD_H
    for r, week in enumerate(weeks):
        for c, day in enumerate(week):
            x, y = PNG_PAD + c * PNG_CELL_W, grid_y + r * PNG_CELL_H
            box = [x, y, x + PNG_CELL_W, y + PNG_CELL_H]
            if day == 0:
                dr.rectangle(box, fill=COLOR_OTHER_MONTH, outline=COLOR_GRID)
                continue
            d = date(year, month, day)
            if d == today:
                dr.rectangle(box, fill=COLOR_TODAY_BG, outline=COLOR_GRID)
                dr.rectangle([x + 2, y + 2, x + PNG_CELL_W - 2, y + PNG_CELL_H - 2], outline=COLOR_TODAY_RING, width=3)
            else:
                dr.rectangle(box, fill=COLOR_BG, outline=COLOR_GRID)
            color = COLOR_SUN if c == 0 else COLOR_SAT if c == 6 else COLOR_TEXT
            dr.text((x + 8, y + 5), str(day), font=f_day, fill=color)
            if d == today:
                dr.text((x + 8 + dr.textlength(str(day), font=f_day) + 6, y + 8), "오늘", font=f_ev, fill=COLOR_TODAY_RING)
            for i, label in enumerate(cell_labels(grouped.get(d, []), d)):
                more = label.startswith("+") and label.endswith("개")
                dr.text((x + 8, y + PNG_DAY_H + i * PNG_LINE_H), _fit(dr, label, f_ev, PNG_CELL_W - 14),
                        font=f_ev, fill=COLOR_MORE if more else COLOR_EVENT)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def month_summary(events: List[Event], start: date, end: date, limit: int) -> str:
    lines = []
    for d, evs in group_by_day(events, start, end).items():
        for ev in evs:
            if d != ev.start.date() and not (start == d):  # 여러 날 일정은 첫날(또는 월 첫날)만
                continue
            lines.append(f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]}) {event_line(ev, d)}")
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit - 20:
            out.append(f"… 외 {len(lines) - i}건")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) if out else "이 달에는 등록된 일정이 없습니다."


# ─────────────────────────────────────────────────────────────────────────────
# 권한 / 공통 응답
# ─────────────────────────────────────────────────────────────────────────────
def can_modify(interaction: discord.Interaction, ev: Event) -> bool:
    if interaction.user.id == ev.creator_id:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


async def _error(interaction: discord.Interaction, text: str):
    text = truncate(text, MESSAGE_LIMIT)
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


_store: Optional[CalendarStore] = None


def get_store() -> CalendarStore:
    global _store
    if _store is None:
        _store = CalendarStore()
    return _store


def _resolve_times(일시: str, 종료: Optional[str], 종일: bool, base: datetime) -> Tuple[datetime, Optional[datetime]]:
    if 종일:
        start = datetime.combine(parse_date_only(일시, base), time.min)
        end = datetime.combine(parse_date_only(종료, base), time.min) if 종료 else None
    else:
        start = parse_time(일시, base)
        end = parse_end_time(종료, start, base) if 종료 else None
    if end is not None and end < start:
        raise TimeParseError("종료 시각이 시작 시각보다 앞설 수 없습니다.")
    if end is not None and not 종일 and end == start:
        end = None
    return start, end


def _event_embed(ev: Event, heading: str) -> discord.Embed:
    embed = discord.Embed(title=truncate(f"{heading} #{ev.id} {ev.title}", EMBED_TITLE_LIMIT), color=0x57F287)
    embed.add_field(name="일시", value=truncate(format_when(ev), EMBED_FIELD_VALUE_LIMIT), inline=False)
    if ev.description:
        embed.add_field(name="설명", value=truncate(ev.description, EMBED_FIELD_VALUE_LIMIT), inline=False)
    if ev.remind_minutes is None:
        remind = "알림 없음"
    elif ev.all_day:
        remind = f"당일 오전 9시 {ev.remind_minutes}분 전 <#{ev.channel_id}>" if ev.remind_minutes else f"당일 오전 9시 <#{ev.channel_id}>"
    else:
        remind = f"{ev.remind_minutes}분 전 <#{ev.channel_id}>" if ev.remind_minutes else f"시작 시각 <#{ev.channel_id}>"
    embed.add_field(name="알림", value=remind, inline=False)
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# 슬래시 명령
# ─────────────────────────────────────────────────────────────────────────────
@app_commands.command(name="일정추가", description="서버 달력에 일정을 추가합니다.")
@app_commands.guild_only()
@app_commands.describe(
    제목="일정 제목 (최대 100자)",
    일시="시작 일시 (예: 내일 오후 3시, 다음주 금요일 7시 / 종일이면 날짜만: 12월 25일)",
    종료="종료 일시 (선택)",
    설명="설명 (선택, 최대 1000자)",
    종일="종일 일정 여부 (날짜만 입력)",
    알림분="시작 몇 분 전에 이 채널에 알릴지 (기본 10, -1 이면 알림 없음)",
)
async def cmd_add(interaction: discord.Interaction, 제목: str, 일시: str, 종료: Optional[str] = None,
                  설명: Optional[str] = None, 종일: bool = False,
                  알림분: app_commands.Range[int, -1, MAX_REMIND_MINUTES] = DEFAULT_REMIND_MINUTES):
    if interaction.guild_id is None:
        return await _error(interaction, "❌ 일정 기능은 서버에서만 사용할 수 있습니다.")
    title = clean_text(제목, TITLE_MAX)
    if not title:
        return await _error(interaction, "❌ 제목을 입력해 주세요.")
    if 알림분 is not None and not -1 <= 알림분 <= MAX_REMIND_MINUTES:
        return await _error(interaction, f"❌ 알림분은 -1 ~ {MAX_REMIND_MINUTES} 사이로 입력해 주세요.")
    try:
        start, end = _resolve_times(일시, 종료, 종일, now_seoul())
    except TimeParseError as e:
        return await _error(interaction, f"❌ {e}")
    ev = get_store().add(
        interaction.guild_id, interaction.channel_id, interaction.user.id, title, start, end,
        clean_text(설명, DESCRIPTION_MAX), 종일, None if 알림분 == -1 else 알림분,
    )
    await interaction.response.send_message(embed=_event_embed(ev, "📅 일정 추가"), allowed_mentions=NO_MENTIONS)


@app_commands.command(name="일정목록", description="서버 일정을 날짜별로 보여줍니다.")
@app_commands.guild_only()
@app_commands.describe(기간="조회할 기간 (기본: 이번주)")
async def cmd_list(interaction: discord.Interaction,
                   기간: Literal["오늘", "이번주", "이번달", "다음달"] = "이번주"):
    if interaction.guild_id is None:
        return await _error(interaction, "❌ 일정 기능은 서버에서만 사용할 수 있습니다.")
    start, end = period_range(기간, now_seoul().date())
    events = get_store().list_range(interaction.guild_id, start, end)
    title = f"🗓️ {기간} 일정 ({start.month}/{start.day} ~ {(end - timedelta(days=1)).month}/{(end - timedelta(days=1)).day})"
    await interaction.response.send_message(embed=build_list_embed(title, events, start, end),
                                            allowed_mentions=NO_MENTIONS)


def build_calendar_message(guild_id: int, year: int, month: int, today: date,
                           image: bool = True) -> Tuple[discord.Embed, Optional[discord.File]]:
    """/달력 응답(임베드 + 선택적 PNG 파일). 이미지 실패 시 텍스트 격자로 폴백하고 안내한다."""
    start, end = month_range(year, month)
    events = get_store().list_range(guild_id, start, end)
    embed = discord.Embed(title=f"📆 {year}년 {month}월", color=0x5865F2)
    file = None
    notice = ""
    if image:
        png = render_month_png(year, month, events, today)
        if png is not None:
            file = discord.File(io.BytesIO(png), filename="calendar.png")
            embed.set_image(url="attachment://calendar.png")
        else:
            notice = "⚠️ 이 환경에는 Pillow 또는 한글 폰트가 없어 이미지 대신 텍스트 달력으로 보여드립니다.\n"
    if file is None:
        days = [d for ev in events for d in ev.days()]
        embed.description = notice + "```\n" + render_month_text(year, month, days, today) + "\n```"
    embed.add_field(name="이 달 일정", value=month_summary(events, start, end, EMBED_FIELD_VALUE_LIMIT), inline=False)
    return embed, file


def shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


class CalendarView(discord.ui.View):
    """이전/다음 달 버튼. 같은 서버의 일정만 다시 그린다."""

    def __init__(self, guild_id: int, year: int, month: int, image: bool = True, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.guild_id, self.year, self.month, self.image = guild_id, year, month, image

    async def _go(self, interaction: discord.Interaction, delta: int):
        if interaction.guild_id != self.guild_id:
            return await _error(interaction, "❌ 다른 서버의 달력은 넘길 수 없습니다.")
        y, m = shift_month(self.year, self.month, delta)
        if not 1900 <= y <= 2200:
            return await _error(interaction, "❌ 더 이상 넘길 수 없습니다.")
        self.year, self.month = y, m
        embed, file = build_calendar_message(self.guild_id, y, m, now_seoul().date(), self.image)
        await interaction.response.edit_message(embed=embed, attachments=[file] if file else [], view=self,
                                                allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="◀ 이전 달", style=discord.ButtonStyle.secondary)
    async def prev_month(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, -1)

    @discord.ui.button(label="오늘", style=discord.ButtonStyle.primary)
    async def this_month(self, interaction: discord.Interaction, button: discord.ui.Button):
        today = now_seoul().date()
        await self._go(interaction, (today.year * 12 + today.month) - (self.year * 12 + self.month))

    @discord.ui.button(label="다음 달 ▶", style=discord.ButtonStyle.secondary)
    async def next_month(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, 1)


@app_commands.command(name="달력", description="월간 달력(칸 안에 일정 표시)을 보여줍니다.")
@app_commands.guild_only()
@app_commands.describe(년="연도 (기본: 올해)", 월="월 (기본: 이번 달)",
                       이미지="PNG 달력 이미지로 보기 (기본: 켜짐, 끄면 텍스트 달력)")
async def cmd_calendar(interaction: discord.Interaction,
                       년: Optional[app_commands.Range[int, 1900, 2200]] = None,
                       월: Optional[app_commands.Range[int, 1, 12]] = None,
                       이미지: bool = True):
    if interaction.guild_id is None:
        return await _error(interaction, "❌ 일정 기능은 서버에서만 사용할 수 있습니다.")
    today = now_seoul().date()
    year = 년 if 년 is not None else today.year
    month = 월 if 월 is not None else today.month
    if not (1 <= month <= 12 and 1900 <= year <= 2200):
        return await _error(interaction, "❌ 연도는 1900~2200, 월은 1~12 사이로 입력해 주세요.")
    embed, file = build_calendar_message(interaction.guild_id, year, month, today, 이미지)
    kwargs = {"embed": embed, "allowed_mentions": NO_MENTIONS,
              "view": CalendarView(interaction.guild_id, year, month, 이미지)}
    if file is not None:
        kwargs["file"] = file
    await interaction.response.send_message(**kwargs)


@app_commands.command(name="일정수정", description="일정을 수정합니다. (만든 사람 또는 서버 관리 권한자)")
@app_commands.guild_only()
@app_commands.describe(
    번호="일정 번호 (/일정목록 의 #번호)", 제목="새 제목", 일시="새 시작 일시", 종료="새 종료 일시 ('없음' 이면 삭제)",
    설명="새 설명 ('없음' 이면 삭제)", 종일="종일 일정 여부", 알림분="시작 몇 분 전 알림 (-1: 알림 없음)",
)
async def cmd_edit(interaction: discord.Interaction, 번호: int, 제목: Optional[str] = None,
                   일시: Optional[str] = None, 종료: Optional[str] = None, 설명: Optional[str] = None,
                   종일: Optional[bool] = None,
                   알림분: Optional[app_commands.Range[int, -1, MAX_REMIND_MINUTES]] = None):
    if interaction.guild_id is None:
        return await _error(interaction, "❌ 일정 기능은 서버에서만 사용할 수 있습니다.")
    store = get_store()
    ev = store.get(interaction.guild_id, 번호)
    if ev is None:
        return await _error(interaction, f"❌ 이 서버에서 일정 #{번호} 을(를) 찾을 수 없습니다.")
    if not can_modify(interaction, ev):
        return await _error(interaction, "❌ 일정을 만든 사람 또는 서버 관리 권한이 있는 사람만 수정할 수 있습니다.")
    if all(v is None for v in (제목, 일시, 종료, 설명, 종일, 알림분)):
        return await _error(interaction, "❌ 수정할 항목을 하나 이상 입력해 주세요.")
    fields = {}
    if 제목 is not None:
        title = clean_text(제목, TITLE_MAX)
        if not title:
            return await _error(interaction, "❌ 제목은 비울 수 없습니다.")
        fields["title"] = title
    if 설명 is not None:
        fields["description"] = "" if 설명.strip() == "없음" else clean_text(설명, DESCRIPTION_MAX)
    if 알림분 is not None:
        if not -1 <= 알림분 <= MAX_REMIND_MINUTES:
            return await _error(interaction, f"❌ 알림분은 -1 ~ {MAX_REMIND_MINUTES} 사이로 입력해 주세요.")
        fields["remind_minutes"] = None if 알림분 == -1 else 알림분
    all_day = ev.all_day if 종일 is None else 종일
    if 일시 is not None or 종료 is not None or 종일 is not None:
        base = now_seoul()
        try:
            if 일시 is not None:
                start = (datetime.combine(parse_date_only(일시, base), time.min) if all_day
                         else parse_time(일시, base))
            elif all_day:
                start = datetime.combine(ev.start.date(), time.min)
            else:
                start = ev.start
            if 종료 is not None and 종료.strip() == "없음":
                end = None
            elif 종료 is not None:
                end = (datetime.combine(parse_date_only(종료, base), time.min) if all_day
                       else parse_end_time(종료, start, base))
            elif ev.end is not None and all_day:
                end = datetime.combine(ev.end.date(), time.min)
            else:
                end = ev.end
        except TimeParseError as e:
            return await _error(interaction, f"❌ {e}")
        if end is not None and end < start:
            return await _error(interaction, "❌ 종료 시각이 시작 시각보다 앞설 수 없습니다.")
        fields.update(start=start, end=end, all_day=all_day)
    ev = store.update(interaction.guild_id, 번호, **fields)
    await interaction.response.send_message(embed=_event_embed(ev, "✏️ 일정 수정"), allowed_mentions=NO_MENTIONS)


@app_commands.command(name="일정삭제", description="일정을 삭제합니다. (만든 사람 또는 서버 관리 권한자)")
@app_commands.guild_only()
@app_commands.describe(번호="일정 번호 (/일정목록 의 #번호)")
async def cmd_delete(interaction: discord.Interaction, 번호: int):
    if interaction.guild_id is None:
        return await _error(interaction, "❌ 일정 기능은 서버에서만 사용할 수 있습니다.")
    store = get_store()
    ev = store.get(interaction.guild_id, 번호)
    if ev is None:
        return await _error(interaction, f"❌ 이 서버에서 일정 #{번호} 을(를) 찾을 수 없습니다.")
    if not can_modify(interaction, ev):
        return await _error(interaction, "❌ 일정을 만든 사람 또는 서버 관리 권한이 있는 사람만 삭제할 수 있습니다.")
    store.delete(interaction.guild_id, 번호)
    await interaction.response.send_message(
        truncate(f"🗑️ 일정 #{ev.id} **{discord.utils.escape_markdown(ev.title)}** 을(를) 삭제했습니다.", MESSAGE_LIMIT),
        allowed_mentions=NO_MENTIONS,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 일괄 등록 / 내보내기
# ─────────────────────────────────────────────────────────────────────────────
BULK_MAX_LINES = 1000
BULK_MAX_BYTES = 512 * 1024
BULK_MODAL_MAX = 4000
BULK_FORMAT_HELP = "형식: `날짜 [시각] | 제목 | 설명(선택) | 알림N분전(선택)` 예) `2026-11-20 14:00 | 팀 회의 | 3층 | 30`"

_BULK_DATE_RE = re.compile(
    r"^(?:(?:(?P<y>\d{4})[-/.])?(?P<m>\d{1,2})[-/.](?P<d>\d{1,2})\.?"
    r"|(?:(?P<ky>\d{4})\s*년\s*)?(?P<km>\d{1,2})\s*월\s*(?P<kd>\d{1,2})\s*일)$"
)
_BULK_KO_DATE_PREFIX_RE = re.compile(r"^((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)\s*(.*)$")
_BULK_TIME_RE = re.compile(
    r"^(?:(?P<ap>오전|오후)\s*)?(?:(?P<h>\d{1,2}):(?P<mi>\d{2})"
    r"|(?P<kh>\d{1,2})\s*시(?:\s*(?P<kmi>\d{1,2})\s*분|\s*(?P<half>반))?)$"
)


def _split_when(when: str) -> Tuple[str, str]:
    """첫 칸 → (날짜, 시각). '11월 20일 오후 2시', '2026-11-20 14:00' 등."""
    when = when.strip()
    m = _BULK_KO_DATE_PREFIX_RE.match(when)
    if m:
        return m.group(1), m.group(2).strip()
    head, _, rest = when.partition(" ")
    return head, rest.strip()


def _bulk_time(time_s: str) -> time:
    s = time_s.strip()
    tm = _BULK_TIME_RE.match(s)
    err = ValueError(f"시각 형식 오류 '{truncate(s, 20)}' (예: 14:00, 오후 2시, 오전 9시 30분)")
    if not tm:
        raise err
    if tm.group("h") is not None:
        h, mi = int(tm.group("h")), int(tm.group("mi"))
    else:
        h = int(tm.group("kh"))
        mi = 30 if tm.group("half") else int(tm.group("kmi") or 0)
    ap = tm.group("ap")
    if ap:
        if not 1 <= h <= 12:
            raise err
        h = h % 12 + (12 if ap == "오후" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise err
    return time(h, mi)
_BULK_REMIND_RE = re.compile(r"^(?:알림)?\s*(?P<n>-?\d+)\s*(?:분)?\s*(?:전)?$")


@dataclass
class BulkItem:
    line_no: int
    start: datetime
    all_day: bool
    title: str
    description: str = ""
    remind_minutes: Optional[int] = DEFAULT_REMIND_MINUTES


class BulkParseError(ValueError):
    """파일 전체를 처리할 수 없을 때 (인코딩, 줄 수 초과 등)."""


def decode_bulk_bytes(data: bytes) -> str:
    """UTF-8(BOM 포함) → CP949 순으로 시도."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise BulkParseError("파일 인코딩을 알 수 없습니다. UTF-8 또는 CP949(EUC-KR)로 저장해 주세요.")


def _bulk_datetime(date_s: str, time_s: str, now: datetime) -> Tuple[datetime, bool]:
    m = _BULK_DATE_RE.match(date_s.strip())
    if not m:
        raise ValueError(f"날짜 형식 오류 '{truncate(date_s.strip(), 20)}' (예: 2026-11-20, 11/27, 11월 27일)")
    if m.group("km") is not None:
        year_s, month, day = m.group("ky"), int(m.group("km")), int(m.group("kd"))
    else:
        year_s, month, day = m.group("y"), int(m.group("m")), int(m.group("d"))
    t = _bulk_time(time_s) if time_s.strip() else None

    def build(year):
        return datetime.combine(date(year, month, day), t or time.min)

    try:
        if year_s:
            dt = build(int(year_s))
        else:  # 연도 생략: 가장 가까운 미래 (종일이면 오늘도 포함)
            dt = None
            for year in range(now.year, now.year + 9):
                try:
                    cand = build(year)
                except ValueError:
                    continue  # 2월 29일 등
                if (cand.date() >= now.date()) if t is None else (cand >= now):
                    dt = cand
                    break
            if dt is None:
                raise ValueError
    except ValueError:
        raise ValueError(f"존재하지 않는 날짜 '{date_s.strip()}'") from None
    return dt, t is None


def _bulk_item(line_no: int, date_s: str, time_s: str, title: str, desc: str, remind: str, now: datetime) -> BulkItem:
    start, all_day = _bulk_datetime(date_s, time_s, now)
    title = clean_text(title, TITLE_MAX)
    if not title:
        raise ValueError("제목이 비어 있습니다")
    remind = (remind or "").strip()
    if remind:
        rm = _BULK_REMIND_RE.match(remind)
        if not rm or not -1 <= int(rm.group("n")) <= MAX_REMIND_MINUTES:
            raise ValueError(f"알림 값 오류 '{truncate(remind, 20)}' (-1 ~ {MAX_REMIND_MINUTES})")
        minutes = int(rm.group("n"))
        remind_minutes = None if minutes == -1 else minutes
    else:
        remind_minutes = DEFAULT_REMIND_MINUTES
    return BulkItem(line_no, start, all_day, title, clean_text(desc, DESCRIPTION_MAX), remind_minutes)


def parse_bulk_text(text: str, now: datetime) -> Tuple[List[BulkItem], List[Tuple[int, str]]]:
    """일괄 등록 텍스트 해석 → (성공 항목, [(줄번호, 사유)]).

    파이프(|) 형식과 CSV(헤더: 날짜,시각,제목,설명,알림) 형식을 모두 지원한다.
    '#' 로 시작하는 줄과 빈 줄은 무시. 데이터 줄이 1000개를 넘으면 BulkParseError.
    """
    import csv

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    data = [(i + 1, ln) for i, ln in enumerate(lines) if ln.strip() and not ln.strip().startswith("#")]
    if not data:
        raise BulkParseError("등록할 일정이 없습니다. " + BULK_FORMAT_HELP)
    header = [h.strip().lstrip("\ufeff") for h in next(csv.reader([data[0][1]]))]
    is_csv = "|" not in data[0][1] and "날짜" in header and "제목" in header
    if is_csv:
        data = data[1:]
    if len(data) > BULK_MAX_LINES:
        raise BulkParseError(f"한 번에 최대 {BULK_MAX_LINES}줄까지 등록할 수 있습니다. (현재 {len(data)}줄)")
    items, errors = [], []
    for line_no, line in data:
        try:
            if is_csv:
                row = next(csv.reader([line]))
                rec = dict(zip(header, row + [""] * (len(header) - len(row)), strict=False))
                items.append(_bulk_item(line_no, rec.get("날짜", ""), rec.get("시각", ""), rec.get("제목", ""),
                                        rec.get("설명", ""), rec.get("알림", ""), now))
            else:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2:
                    raise ValueError("'|' 로 날짜와 제목을 구분해 주세요")
                if len(parts) > 4:
                    raise ValueError("칸이 너무 많습니다 (최대 4칸)")
                parts += [""] * (4 - len(parts))
                date_s, time_s = _split_when(parts[0])
                items.append(_bulk_item(line_no, date_s, time_s, parts[1], parts[2], parts[3], now))
        except ValueError as e:
            errors.append((line_no, str(e) or "형식 오류"))
    return items, errors


def _export_field(s: str) -> str:
    return (s or "").replace("|", "｜").replace("\r", " ").replace("\n", " ").strip()


def export_events_text(events: List[Event]) -> str:
    """일괄 등록과 같은 형식으로 내보내기 (종료 일시는 형식에 없어 제외)."""
    out = ["# 날짜 [시각] | 제목 | 설명 | 알림(분 전, -1=없음)"]
    for ev in events:
        when = ev.start.strftime("%Y-%m-%d") if ev.all_day else ev.start.strftime("%Y-%m-%d %H:%M")
        remind = -1 if ev.remind_minutes is None else ev.remind_minutes
        out.append(f"{when} | {_export_field(ev.title)} | {_export_field(ev.description)} | {remind}")
    return "\n".join(out) + "\n"


def build_bulk_preview(items: List[BulkItem], errors: List[Tuple[int, str]]) -> discord.Embed:
    color = 0x57F287 if items and not errors else 0xFEE75C if items else 0xED4245
    embed = discord.Embed(title="📥 일정 일괄 등록 미리보기", color=color)
    embed.description = f"✅ 등록 가능 **{len(items)}건** · ❌ 오류 **{len(errors)}줄**"
    if items:
        sample = []
        for it in items[:10]:
            when = it.start.strftime("%Y-%m-%d") + (" (종일)" if it.all_day else it.start.strftime(" %H:%M"))
            sample.append(truncate(f"{it.line_no}줄: {when} · {discord.utils.escape_markdown(it.title)}", 100))
        if len(items) > 10:
            sample.append(f"… 외 {len(items) - 10}건")
        embed.add_field(name="앞부분 샘플", value=truncate("\n".join(sample), EMBED_FIELD_VALUE_LIMIT), inline=False)
    if errors:
        lines, used = [], 0
        for i, (n, why) in enumerate(errors):
            line = truncate(f"{n}줄: {why}", 120)
            if used + len(line) + 1 > EMBED_FIELD_VALUE_LIMIT - 20:
                lines.append(f"… 외 {len(errors) - i}줄")
                break
            lines.append(line)
            used += len(line) + 1
        embed.add_field(name="오류 줄", value="\n".join(lines), inline=False)
    embed.set_footer(text="같은 제목+같은 일시의 일정은 등록 시 건너뜁니다. 오류 줄은 제외하고 등록됩니다.")
    return embed


class BulkConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int, channel_id: int, items: List[BulkItem], timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id, self.guild_id, self.channel_id, self.items = owner_id, guild_id, channel_id, items
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _error(interaction, "❌ 일괄 등록을 시작한 사람만 누를 수 있습니다.")
            return False
        return True

    async def do_confirm(self, interaction: discord.Interaction):
        if self.done:
            return await _error(interaction, "이미 처리된 요청입니다.")
        self.done = True
        self.stop()
        added, skipped = get_store().bulk_add(self.guild_id, self.channel_id, self.owner_id, self.items)
        await interaction.response.edit_message(
            content=f"✅ 일정 {added}건을 등록했습니다." + (f" (중복 {skipped}건 건너뜀)" if skipped else ""),
            embed=None, view=None, allowed_mentions=NO_MENTIONS)

    async def do_cancel(self, interaction: discord.Interaction):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="취소했습니다.", embed=None, view=None)

    @discord.ui.button(label="등록", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.do_confirm(interaction)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.do_cancel(interaction)


async def _send_bulk_preview(interaction: discord.Interaction, text: str):
    try:
        items, errors = parse_bulk_text(text, now_seoul())
    except BulkParseError as e:
        return await _error(interaction, f"❌ {e}")
    view = BulkConfirmView(interaction.user.id, interaction.guild_id, interaction.channel_id, items) if items else None
    kwargs = {"embed": build_bulk_preview(items, errors), "ephemeral": True, "allowed_mentions": NO_MENTIONS}
    if view is not None:
        kwargs["view"] = view
    await interaction.response.send_message(**kwargs)


class BulkModal(discord.ui.Modal, title="일정 일괄 등록"):
    body = discord.ui.TextInput(label="한 줄에 일정 하나 (날짜 [시각] | 제목 | 설명 | 알림)",
                                style=discord.TextStyle.paragraph, max_length=BULK_MODAL_MAX,
                                placeholder="2026-11-20 14:00 | 팀 회의 | 3층 회의실 | 30\n2026-11-25 | 창립기념일")

    async def on_submit(self, interaction: discord.Interaction):
        await _send_bulk_preview(interaction, str(self.body.value))


@app_commands.command(name="일정일괄추가", description="여러 일정을 한 번에 등록합니다 (.txt/.csv 첨부 또는 입력창).")
@app_commands.guild_only()
@app_commands.describe(파일="한 줄에 일정 하나: 날짜 [시각] | 제목 | 설명 | 알림  (없으면 입력창이 열림)")
async def cmd_bulk_add(interaction: discord.Interaction, 파일: Optional[discord.Attachment] = None):
    if interaction.guild_id is None:
        return await _error(interaction, "❌ 일정 기능은 서버에서만 사용할 수 있습니다.")
    if 파일 is None:
        return await interaction.response.send_modal(BulkModal())
    name = (파일.filename or "").lower()
    if not name.endswith((".txt", ".csv")):
        return await _error(interaction, "❌ .txt 또는 .csv 파일만 올릴 수 있습니다.")
    if 파일.size > BULK_MAX_BYTES:
        return await _error(interaction, f"❌ 파일이 너무 큽니다. (최대 {BULK_MAX_BYTES // 1024}KB)")
    try:
        text = decode_bulk_bytes(await 파일.read())
    except BulkParseError as e:
        return await _error(interaction, f"❌ {e}")
    except discord.HTTPException:
        return await _error(interaction, "❌ 첨부 파일을 읽지 못했습니다. 다시 시도해 주세요.")
    await _send_bulk_preview(interaction, text)


@app_commands.command(name="일정내보내기", description="일정을 일괄 등록과 같은 형식의 .txt 파일로 내보냅니다.")
@app_commands.guild_only()
@app_commands.describe(기간="내보낼 기간 (기본: 이번달)")
async def cmd_export(interaction: discord.Interaction,
                     기간: Literal["오늘", "이번주", "이번달", "다음달", "전체"] = "이번달"):
    if interaction.guild_id is None:
        return await _error(interaction, "❌ 일정 기능은 서버에서만 사용할 수 있습니다.")
    if 기간 == "전체":
        start, end = date(1900, 1, 1), date(2201, 1, 1)
    else:
        start, end = period_range(기간, now_seoul().date())
    events = get_store().list_range(interaction.guild_id, start, end)
    data = export_events_text(events).encode("utf-8")
    await interaction.response.send_message(
        f"📤 {기간} 일정 {len(events)}건을 내보냈습니다. `/일정일괄추가` 로 다시 불러올 수 있습니다.",
        file=discord.File(io.BytesIO(data), filename="일정.txt"), ephemeral=True, allowed_mentions=NO_MENTIONS)


COMMANDS = (cmd_add, cmd_list, cmd_calendar, cmd_edit, cmd_delete, cmd_bulk_add, cmd_export)


# ─────────────────────────────────────────────────────────────────────────────
# 알림 루프
# ─────────────────────────────────────────────────────────────────────────────
def reminder_text(ev: Event) -> str:
    if ev.all_day:
        head = "📅 오늘은 종일 일정이 있습니다" if not ev.remind_minutes else "📅 종일 일정 알림"
    elif ev.remind_minutes:
        head = f"⏰ {ev.remind_minutes}분 뒤 일정이 시작됩니다"
    else:
        head = "⏰ 일정이 지금 시작됩니다"
    body = f"{head}: **{discord.utils.escape_markdown(ev.title)}** (#{ev.id})\n🕒 {format_when(ev)}"
    if ev.description:
        body += f"\n📝 {ev.description}"
    return truncate(body, MESSAGE_LIMIT)


async def process_reminders(bot, store: CalendarStore, now: datetime) -> int:
    """기한이 된 알림을 발송하고 보낸 개수를 돌려준다. 오래 지난 알림은 발송 없이 처리 표시."""
    sent = 0
    for ev in store.due_reminders(now):
        if not store.claim_reminder(ev.id):
            continue  # 다른 루프가 이미 처리
        remind_at = store.remind_at(ev.id)
        if remind_at is not None and now - remind_at > REMIND_GRACE:
            continue  # 봇이 꺼져 있던 사이 지나간 알림은 뒤늦게 보내지 않음
        try:
            channel = bot.get_channel(ev.channel_id) or await bot.fetch_channel(ev.channel_id)
            await channel.send(reminder_text(ev), allowed_mentions=NO_MENTIONS)
            sent += 1
        except Exception as e:  # 한 건 실패가 루프 전체를 멈추지 않도록
            print(f"⚠️ 일정 알림 전송 실패 (event={ev.id}, channel={ev.channel_id}): {type(e).__name__}: {e}")
    return sent


async def reminder_loop(bot, store_getter=get_store, interval: float = POLL_INTERVAL_SECONDS):
    while not bot.is_closed():
        try:
            await process_reminders(bot, store_getter(), now_seoul())
        except Exception as e:
            print(f"⚠️ 일정 알림 루프 오류: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


def register(bot) -> None:
    """슬래시 명령을 bot.tree 에 등록하고, 준비되면 알림 루프를 시작한다."""
    for cmd in COMMANDS:
        bot.tree.add_command(cmd)

    async def _start_reminder_loop():
        task = getattr(bot, "_calendar_reminder_task", None)
        if task is None or task.done():  # on_ready 는 재연결 때마다 호출되므로 한 번만 시작
            bot._calendar_reminder_task = asyncio.create_task(reminder_loop(bot))
            print("📅 일정 알림 루프가 시작되었습니다.")

    bot.add_listener(_start_reminder_loop, "on_ready")
