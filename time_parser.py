"""알림 예약용 시간/날짜 텍스트 파서.

사용자가 `/예약` 명령의 `일시` 칸에 입력한 한국어/영어/숫자 형식의 시간 표현을
**한국 시간(Asia/Seoul) 기준 naive datetime** 으로 변환한다.

공개 API
--------
- ``parse_time(text, base_time) -> datetime``
    해석에 성공하면 datetime 을, 실패하면 ``TimeParseError`` (사용자에게 그대로
    보여줄 수 있는 한국어 메시지 포함)를 던진다.
- ``parse_korean_time(text, base_time) -> Optional[datetime]``
    기존 bot.py 호출부와의 호환용 래퍼. 실패하면 ``None`` 을 돌려준다.
- ``is_recurring(text) -> bool`` / ``parse_recurrence(text) -> Recurrence``
    '매일 오전 7시', '매주 월수금 9시', '2시간마다' 같은 반복 표현 해석.
    (``parse_time`` 은 반복 표현을 계속 거부한다.)

설계 원칙
---------
- 외부 라이브러리(dateparser 등)에 기대지 않고 결정적으로 동작한다.
- 입력 문자열을 "날짜 / 시각 / 상대시간" 조각으로 나눠 해석하고, 해석하지 못한
  글자가 하나라도 남으면 **추측하지 않고 실패**한다. (예: "내일 3시 회의")
- 날짜 없이 시각만 입력했는데 그 시각이 이미 지났다면 가장 가까운 미래로 넘긴다.
- '오늘'이나 특정 날짜를 명시했는데 이미 지난 시각이면 그대로 돌려준다.
  (호출부에서 "과거 시간" 안내를 띄우도록)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

__all__ = [
    "TimeParseError", "parse_time", "parse_korean_time",
    "Recurrence", "parse_recurrence", "is_recurring", "MIN_INTERVAL_SECONDS",
]


class TimeParseError(ValueError):
    """시간 텍스트를 해석할 수 없을 때 발생. ``str(e)`` 는 사용자용 한국어 메시지."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. 정규화
# ─────────────────────────────────────────────────────────────────────────────

# 한글 고유어 수사 (시 / 시간 앞에서 사용)
_NATIVE_NUMS = {
    "스물네": 24, "스물세": 23, "스물두": 22, "스물한": 21, "스무": 20, "스물": 20,
    "열아홉": 19, "열여덟": 18, "열일곱": 17, "열여섯": 16, "열다섯": 15,
    "열네": 14, "열세": 13, "열두": 12, "열한": 11, "열": 10,
    "아홉": 9, "여덟": 8, "일곱": 7, "여섯": 6, "다섯": 5,
    "네": 4, "세": 3, "두": 2, "한": 1,
}
_NATIVE_RE = re.compile(
    "(" + "|".join(sorted(_NATIVE_NUMS, key=len, reverse=True)) + r")\s*(시간|시)"
)

_SINO_DIGITS = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
# 한자어 수사 1~99 (분 / 초 / 시 / 시간 / 일 / 주 / 월 앞에서 사용)
_SINO_RE = re.compile(
    r"(?<![내요주매금명평휴생])((?:[이삼사오육칠팔구]?십)?[일이삼사오육칠팔구]?)\s*(시간|분|초|시|일|주|월)"
)


def _sino_to_int(s: str) -> int:
    if "십" in s:
        tens, _, ones = s.partition("십")
        return (_SINO_DIGITS[tens] if tens else 1) * 10 + (_SINO_DIGITS[ones] if ones else 0)
    return _SINO_DIGITS[s]


def _replace_sino(m: re.Match) -> str:
    num, unit = m.group(1), m.group(2)
    if not num:
        return m.group(0)
    # '일주일'(=1주일)은 그대로 둔다 → 단위 처리에서 '주일'로 해석
    return f"{_sino_to_int(num)}{unit}"


# 고유어 날짜 수 (이틀 뒤, 사흘 후 …)
_NATIVE_DAYS = {"하루": 1, "이틀": 2, "사흘": 3, "나흘": 4, "닷새": 5, "엿새": 6, "이레": 7, "열흘": 10}
_NATIVE_DAYS_RE = re.compile("(" + "|".join(_NATIVE_DAYS) + ")")

# 흔한 오타/줄임말 → 표준 표현
_TYPOS = [("내일모래", "내일모레"), ("낼모래", "낼모레"), ("모래", "모레"), ("오우", "오후"), ("오훟", "오후")]

_RECURRING_RE = re.compile(
    r"매일|매주|매달|매월|매년|매시간|평일마다|주말마다|마다|everyday|every|daily|weekly|monthly"
)


def _normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).lower().strip()
    s = s.replace("a.m.", "am").replace("p.m.", "pm").replace("a.m", "am").replace("p.m", "pm")
    # 숫자 사이가 아닌 마침표·문장부호 제거 (2026.06.02 의 점은 보존)
    s = re.sub(r"[!?~,'\"()\[\]]", " ", s)
    # "2026. 12. 25." → "2026.12.25"
    s = re.sub(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?", r"\1.\2.\3 ", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)
    s = re.sub(r"\s*:\s*", ":", s)  # "15 : 30" → "15:30"

    for wrong, right in _TYPOS:
        s = s.replace(wrong, right)

    # 한글 수사 → 아라비아 숫자
    s = _NATIVE_DAYS_RE.sub(lambda m: f"{_NATIVE_DAYS[m.group(1)]}일", s)
    s = _NATIVE_RE.sub(lambda m: f"{_NATIVE_NUMS[m.group(1)]}{m.group(2)}", s)
    s = _SINO_RE.sub(_replace_sino, s)
    s = re.sub(r"(?<![가-힣\d])반\s*시간", "30분", s)  # "반시간 뒤"

    # 공백 제거: 숫자-숫자 사이 공백만 남긴다 ("6/2 15:00" 의 경계 보존)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?<!\d) | (?!\d)", "", s)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# 2. 패턴
# ─────────────────────────────────────────────────────────────────────────────

_NUM = r"\d+(?:\.\d+)?"

_KO_UNIT = r"(?:주일|주|일|시간|분|초)"
_EN_UNIT = (
    r"(?:weeks?|wks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)"
)

_REL_KO_RE = re.compile(
    rf"(?P<dur>(?:{_NUM}{_KO_UNIT}반?)+)(?:뒤에|후에|뒤|후|있다가|이따가|이따|지나서)"
)
_EN_DUR = rf"(?:(?:{_NUM}|an|a|halfan|half)(?:{_EN_UNIT})(?:and)?)+"
_REL_EN_RE = re.compile(
    rf"(?:in(?P<dur1>{_EN_DUR}))|(?:(?P<dur2>{_EN_DUR})(?:later|fromnow))|(?:after(?P<dur3>{_EN_DUR}))"
)
_DUR_PART_KO = re.compile(rf"({_NUM})({_KO_UNIT})(반)?")
_DUR_PART_EN = re.compile(rf"({_NUM}|an|a|halfan|half)({_EN_UNIT})")

_DAY_WORDS = [
    ("thedayaftertomorrow", 2), ("dayaftertomorrow", 2),
    ("내일모레", 2), ("낼모레", 2), ("모레", 2), ("글피", 3),
    ("내일", 1), ("낼", 1), ("명일", 1), ("tomorrow", 1), ("tmrw", 1), ("tmr", 1),
    ("오늘", 0), ("금일", 0), ("today", 0), ("tonight", 0),
    ("어제", -1), ("yesterday", -1),
]
_DAY_WORD_RE = re.compile("|".join(w for w, _ in _DAY_WORDS))
_DAY_WORD_OFFSET = dict(_DAY_WORDS)

_KO_WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_EN_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_WEEK_PREFIX = r"(?P<pre>다다음주|다음주|담주|차주|이번주|금주|이번|다음|nextweek|thisweek|next|this)?"
_WEEKDAY_RE = re.compile(
    _WEEK_PREFIX
    + r"(?:(?P<ko>[월화수목금토일])(?:요일|욜)"
    + r"|(?P<en>" + "|".join(sorted(_EN_WEEKDAYS, key=len, reverse=True)) + r"))"
)

_DATE_YMD_RE = re.compile(
    r"(?<!\d)(?P<y>\d{4})(?:년(?P<m1>\d{1,2})월(?P<d1>\d{1,2})일?"
    r"|[-./](?P<m2>\d{1,2})[-./](?P<d2>\d{1,2})(?!\d))"
)
_DATE_MD_RE = re.compile(r"(?<![\d.:/-])(?:(?P<m1>\d{1,2})월(?P<d1>\d{1,2})일|(?P<m2>\d{1,2})/(?P<d2>\d{1,2})(?![\d:/]))")
_DATE_D_RE = re.compile(r"(?P<pre>다음달|담달|이번달)?(?<![\d.:/-])(?P<d>\d{1,2})일")

_MERIDIEM = r"(?:오전|오후|새벽|아침|점심|낮|저녁|밤|am|pm|morning|afternoon|evening|night)"
_TIME_RES = [
    # 15:30, 15:30:20, 오후3:30, 3:30pm
    re.compile(
        rf"(?P<mer>{_MERIDIEM})?(?P<h>\d{{1,2}}):(?P<mi>\d{{2}})(?::(?P<s>\d{{2}}))?(?P<mer2>am|pm|오전|오후)?(?!\d)"
    ),
    # 3시, 3시30분, 3시반, 3시30분20초, 오후3시
    re.compile(
        rf"(?P<mer>{_MERIDIEM})?(?P<h>\d{{1,2}})시(?:(?P<half>반)|(?P<mi>\d{{1,2}})분|(?P<mi2>\d{{2}})(?![\d분초시일주]))?(?:(?P<s>\d{{1,2}})초)?(?P<mer2>오전|오후)?"
    ),
    # 7am, 7pm, at7pm
    re.compile(r"(?<![\d:])(?P<h>\d{1,2})(?P<mer2>am|pm)"),
    # 0730, 1530 (4자리 24시간제)
    re.compile(r"(?<![\d:/.-])(?P<hhmm>\d{4})(?![\d:/.-])"),
    # 오후3, morning7 (오전/오후 등 + 숫자만)
    re.compile(rf"(?P<mer>{_MERIDIEM})(?P<h>\d{{1,2}})(?![\d:시])"),
]
_SPECIAL_TIME_RE = re.compile(r"자정|정오|한밤중|midnight|noon")
_LONE_MERIDIEM_RE = re.compile(_MERIDIEM)

_FILLER_RE = re.compile(r"정각|쯤|경에|경|까지|에|at|on|the|of")


# ─────────────────────────────────────────────────────────────────────────────
# 3. 해석 결과 구조
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Clock:
    hour: int
    minute: int = 0
    second: int = 0
    meridiem: Optional[str] = None  # 정규화된 오전/오후 계열 단어
    special: Optional[str] = None   # "midnight" / "noon"
    is24: bool = False              # "0730", "07:30", "15시" 처럼 24시간제가 명백한 경우


@dataclass
class _DateSpec:
    kind: str                 # "offset" | "weekday" | "ymd" | "md" | "d"
    offset: int = 0
    weekday: int = 0
    week_prefix: Optional[str] = None
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    explicit_today: bool = False


_MER_ALIASES = {
    "am": "오전", "morning": "아침",
    "pm": "오후", "afternoon": "오후", "evening": "저녁", "night": "밤",
}


_MER_CLASS = {
    "오전": "am", "새벽": "am", "아침": "am", "am": "am", "morning": "am",
    "오후": "pm", "저녁": "pm", "pm": "pm", "afternoon": "pm", "evening": "pm",
}


class _Text:
    """매칭된 부분을 지워가며 남은 글자를 추적한다."""

    def __init__(self, s: str):
        self.s = s

    def take(self, regex: re.Pattern) -> Optional[re.Match]:
        m = regex.search(self.s)
        if m:
            self.s = self.s[: m.start()] + " " + self.s[m.end():]
        return m

    def take_all(self, regex: re.Pattern) -> list:
        found = []
        while True:
            m = self.take(regex)
            if not m:
                return found
            found.append(m)

    def leftover(self) -> str:
        rest = _FILLER_RE.sub("", self.s)
        return re.sub(r"\s+", " ", rest).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 4. 조각 파서
# ─────────────────────────────────────────────────────────────────────────────

def _parse_duration(dur: str, korean: bool) -> timedelta:
    total = timedelta()
    if not korean:
        dur = dur.replace("and", "")
    parts = (_DUR_PART_KO if korean else _DUR_PART_EN).findall(dur)
    for part in parts:
        if korean:
            num_s, unit, half = part
        else:
            (num_s, unit), half = part, ""
        if num_s in ("a", "an"):
            num = 1.0
        elif num_s in ("half", "halfan"):
            num = 0.5
        else:
            num = float(num_s)
        if unit in ("주일", "주") or unit[0] == "w":
            delta = timedelta(weeks=num)
            if half:
                delta += timedelta(days=3, hours=12)
        elif unit == "일" or unit[0] == "d":
            delta = timedelta(days=num)
            if half:
                delta += timedelta(hours=12)
        elif unit == "시간" or unit[0] == "h":
            delta = timedelta(hours=num)
            if half:
                delta += timedelta(minutes=30)
        elif unit == "분" or (unit[0] == "m"):
            delta = timedelta(minutes=num)
            if half:
                delta += timedelta(seconds=30)
        else:  # 초 / s
            delta = timedelta(seconds=num)
        total += delta
    return total


def _only_days(dur: timedelta) -> bool:
    return dur.seconds == 0 and dur.microseconds == 0


def _parse_clock(m: re.Match) -> _Clock:
    g = m.groupdict()
    if g.get("hhmm"):
        hh, mm = int(g["hhmm"][:2]), int(g["hhmm"][2:])
        return _Clock(hour=hh, minute=mm, is24=True)
    mer = g.get("mer")
    mer2 = g.get("mer2")
    if mer and mer2:
        c1, c2 = _MER_CLASS.get(mer), _MER_CLASS.get(mer2)
        if c1 and c2 and c1 != c2:
            raise TimeParseError("오전/오후 표현이 서로 맞지 않습니다.")
    meridiem = mer or mer2
    meridiem = _MER_ALIASES.get(meridiem, meridiem) if meridiem else None
    mi = g.get("mi") or g.get("mi2")
    minute = 30 if g.get("half") else int(mi) if mi else 0
    second = int(g["s"]) if g.get("s") else 0
    h_raw = g["h"]
    is24 = meridiem is None and (
        (len(h_raw) == 2 and h_raw.startswith("0"))
        or int(h_raw) >= 13
        or (":" in m.group(0) and int(h_raw) == 12)  # "12:00" 은 정오
    )
    return _Clock(hour=int(h_raw), minute=minute, second=second, meridiem=meridiem, is24=is24)


def _resolve_meridiem(hour: int, meridiem: str) -> int:
    """오전/오후 계열 단어 + 12시간제 시각 → 24시간제 시(0~24). 24는 다음날 0시."""
    if not 0 <= hour <= 12:
        raise TimeParseError(f"'{meridiem}' 뒤에는 0~12시만 쓸 수 있습니다. (입력: {hour}시)")
    if meridiem in ("오전", "새벽", "아침"):
        return 0 if hour == 12 else hour
    if meridiem == "오후":
        return 12 if hour == 12 else hour + 12
    if meridiem == "낮":
        return hour if hour >= 10 else hour + 12
    if meridiem == "점심":
        return hour if hour >= 11 else hour + 12
    if meridiem == "저녁":
        if hour == 12 or hour == 0:
            raise TimeParseError("'저녁 12시'는 모호합니다. '자정' 또는 '밤 12시'로 입력해 주세요.")
        return hour + 12
    if meridiem == "밤":
        if hour == 12 or hour == 0:
            return 24
        return hour if hour <= 5 else hour + 12  # 밤 1시 = 다음날 01시
    raise TimeParseError(f"알 수 없는 시간대 표현입니다: {meridiem}")


def _validate_clock(c: _Clock) -> None:
    if not 0 <= c.minute <= 59:
        raise TimeParseError(f"분은 0~59 사이여야 합니다. (입력: {c.minute}분)")
    if not 0 <= c.second <= 59:
        raise TimeParseError(f"초는 0~59 사이여야 합니다. (입력: {c.second}초)")
    if c.meridiem is None and not 0 <= c.hour <= 24:
        raise TimeParseError(f"시는 0~24 사이여야 합니다. (입력: {c.hour}시)")
    if c.hour == 24 and (c.minute or c.second):
        raise TimeParseError("24시 뒤에는 분/초를 붙일 수 없습니다.")


def _at(d: date, hour24: int, c: _Clock) -> datetime:
    """hour24 가 24이면 다음날 0시로 넘긴다. 밤 1~5시도 다음날로 넘긴다."""
    extra_day = 0
    if hour24 >= 24:
        hour24 -= 24
        extra_day = 1
    elif c.meridiem == "밤" and hour24 <= 5:
        extra_day = 1
    return datetime(d.year, d.month, d.day, hour24, c.minute, c.second) + timedelta(days=extra_day)


def _clock_on_date(d: date, c: _Clock) -> datetime:
    """특정 날짜가 정해진 상태에서 시각을 결정 (오전/오후 미지정 시 휴리스틱)."""
    if c.special == "midnight":
        return datetime(d.year, d.month, d.day) + timedelta(days=1)
    if c.special == "noon":
        return datetime(d.year, d.month, d.day, 12)
    if c.meridiem:
        return _at(d, _resolve_meridiem(c.hour, c.meridiem), c)
    h = c.hour
    if not c.is24 and 1 <= h <= 6:
        # "내일 3시" → 15시 (새벽 1~6시는 '새벽/오전'을 명시해야 함)
        h += 12
    return _at(d, h, c)


def _candidates_today(base: datetime, c: _Clock) -> list:
    d = base.date()
    if c.special == "midnight":
        return [datetime(d.year, d.month, d.day) + timedelta(days=1)]
    if c.special == "noon":
        return [datetime(d.year, d.month, d.day, 12)]
    if c.meridiem:
        return [_at(d, _resolve_meridiem(c.hour, c.meridiem), c)]
    if c.is24:
        return [_at(d, c.hour, c)]
    if 1 <= c.hour <= 11:
        return [_at(d, c.hour, c), _at(d, c.hour + 12, c)]
    if c.hour == 12:
        return [_at(d, 12, c), _at(d, 24, c)]
    return [_at(d, c.hour, c)]


# ─────────────────────────────────────────────────────────────────────────────
# 5. 메인 파서
# ─────────────────────────────────────────────────────────────────────────────

_HELP = "예: '내일 오후 3시', '30분 뒤', '15:30', '다음주 월요일 9시'"


def parse_time(text: str, base_time: datetime) -> datetime:
    """시간 텍스트를 해석해 한국 시간 기준 naive ``datetime`` 을 돌려준다.

    :param text: 사용자가 입력한 문자열
    :param base_time: 기준 시각 (보통 현재 한국 시간). tz-aware 여도 되며 그대로
                      벽시계 시각만 사용한다.
    :raises TimeParseError: 해석할 수 없거나 모호한 경우
    """
    if text is None or not str(text).strip():
        raise TimeParseError(f"시간을 입력해 주세요. {_HELP}")

    base = base_time.replace(tzinfo=None, microsecond=0)
    s = _normalize(str(text))

    if _RECURRING_RE.search(s):
        raise TimeParseError("반복 예약('매일', '매주' 등)은 아직 지원하지 않습니다. 한 번만 울릴 시간을 입력해 주세요.")

    t = _Text(s)

    # ── 상대 시간 ────────────────────────────────────────────────────────
    rel: Optional[timedelta] = None
    rel_matches = t.take_all(_REL_KO_RE)
    for m in rel_matches:
        rel = (rel or timedelta()) + _parse_duration(m.group("dur"), korean=True)
    for m in t.take_all(_REL_EN_RE):
        dur = m.group("dur1") or m.group("dur2") or m.group("dur3")
        rel = (rel or timedelta()) + _parse_duration(dur, korean=False)
    if rel is not None and rel <= timedelta():
        raise TimeParseError("0보다 긴 시간을 입력해 주세요.")

    # ── 날짜 ─────────────────────────────────────────────────────────────
    dates: list = []
    tonight = False
    for m in t.take_all(_DATE_YMD_RE):
        dates.append(_DateSpec("ymd", year=int(m["y"]), month=int(m["m1"] or m["m2"]), day=int(m["d1"] or m["d2"])))
    for m in t.take_all(_DATE_MD_RE):
        dates.append(_DateSpec("md", month=int(m["m1"] or m["m2"]), day=int(m["d1"] or m["d2"])))
    for m in t.take_all(_DAY_WORD_RE):
        w = m.group(0)
        tonight = tonight or w == "tonight"
        dates.append(_DateSpec("offset", offset=_DAY_WORD_OFFSET[w], explicit_today=w in ("오늘", "금일", "today", "tonight")))
    weekday_specs = []
    for m in t.take_all(_WEEKDAY_RE):
        wd = _KO_WEEKDAYS[m["ko"]] if m["ko"] else _EN_WEEKDAYS[m["en"]]
        weekday_specs.append(_DateSpec("weekday", weekday=wd, week_prefix=m["pre"]))
    for m in t.take_all(_DATE_D_RE):
        dates.append(_DateSpec("d", day=int(m["d"]), week_prefix=m["pre"]))

    # 날짜 + 요일이 함께 오면 ("6월 2일 화요일") 일관성만 검사
    if weekday_specs and dates:
        if len(weekday_specs) > 1 or weekday_specs[0].week_prefix:
            raise TimeParseError("날짜가 여러 개 입력되었습니다. 하나만 입력해 주세요.")
        check_weekday = weekday_specs[0].weekday
    else:
        check_weekday = None
        dates.extend(weekday_specs)

    if len(dates) > 1:
        raise TimeParseError("날짜가 여러 개 입력되었습니다. 하나만 입력해 주세요.")
    date_spec = dates[0] if dates else None

    # ── 시각 ─────────────────────────────────────────────────────────────
    clocks: list = []
    for m in t.take_all(_SPECIAL_TIME_RE):
        w = m.group(0)
        if w in ("자정", "한밤중", "midnight"):
            clocks.append(_Clock(hour=0, special="midnight"))
        else:
            clocks.append(_Clock(hour=12, special="noon"))
    for regex in _TIME_RES:
        for m in t.take_all(regex):
            clocks.append(_parse_clock(m))
    lone_mer = t.take_all(_LONE_MERIDIEM_RE)

    if len(clocks) > 1:
        raise TimeParseError("시각이 여러 개 입력되었습니다. 하나만 입력해 주세요.")
    clock = clocks[0] if clocks else None
    if clock is not None:
        if lone_mer:
            if clock.meridiem or clock.special:
                raise TimeParseError("시간대 표현(오전/오후 등)이 중복되었습니다.")
            if len(lone_mer) > 1:
                raise TimeParseError("시간대 표현(오전/오후 등)이 중복되었습니다.")
            mer = lone_mer[0].group(0)
            clock.meridiem = _MER_ALIASES.get(mer, mer)
        elif tonight and not clock.meridiem and not clock.special:
            clock.meridiem = "밤" if clock.hour == 12 else "오후"
        _validate_clock(clock)
    elif lone_mer:
        raise TimeParseError(f"몇 시인지 함께 입력해 주세요. {_HELP}")

    rest = t.leftover()
    if rest:
        raise TimeParseError(f"이해할 수 없는 부분이 있습니다: '{rest}'. {_HELP}")

    # ── 조합 ─────────────────────────────────────────────────────────────
    if rel is not None:
        if date_spec is not None:
            raise TimeParseError("상대 시간('~뒤')과 날짜를 함께 쓸 수 없습니다.")
        if clock is None:
            return base + rel
        if not _only_days(rel):
            raise TimeParseError("'~시간/분 뒤'와 시각을 함께 쓸 수 없습니다. 둘 중 하나만 입력해 주세요.")
        return _clock_on_date((base + rel).date(), clock)

    if date_spec is None:
        if clock is None:
            raise TimeParseError(f"시간을 이해하지 못했습니다. {_HELP}")
        # 날짜 미지정: 가장 가까운 미래 시각
        for cand in sorted(_candidates_today(base, clock)):
            if cand > base:
                return cand
        return min(_candidates_today(base, clock)) + timedelta(days=1)

    if clock is None:
        raise TimeParseError(f"날짜만으로는 예약할 수 없습니다. 시각도 함께 입력해 주세요. {_HELP}")

    today = base.date()
    try:
        if date_spec.kind == "offset":
            d = today + timedelta(days=date_spec.offset)
            if check_weekday is not None and d.weekday() != check_weekday:
                raise TimeParseError(
                    f"{d.month}월 {d.day}일은 {'월화수목금토일'[d.weekday()]}요일입니다. 날짜와 요일을 다시 확인해 주세요."
                )
            if date_spec.explicit_today:
                cands = sorted(_candidates_today(base, clock))
                for cand in cands:
                    if cand > base:
                        return cand
                return cands[-1]  # 모두 지났으면 가장 최근 시각 그대로 → 호출부에서 과거 안내
            return _clock_on_date(d, clock)

        if date_spec.kind == "weekday":
            pre = date_spec.week_prefix
            monday = today - timedelta(days=today.weekday())
            if pre in ("다음주", "담주", "차주", "nextweek", "next", "다음"):
                return _clock_on_date(monday + timedelta(days=7 + date_spec.weekday), clock)
            if pre == "다다음주":
                return _clock_on_date(monday + timedelta(days=14 + date_spec.weekday), clock)
            if pre in ("이번주", "금주", "이번", "thisweek"):
                return _clock_on_date(monday + timedelta(days=date_spec.weekday), clock)
            # 접두어 없음 / "this": 다가오는 해당 요일 (오늘이고 지났으면 다음 주)
            d = today + timedelta(days=(date_spec.weekday - today.weekday()) % 7)
            result = _clock_on_date(d, clock)
            if result <= base:
                result = _clock_on_date(d + timedelta(days=7), clock)
            return result

        if date_spec.kind == "ymd":
            d = date(date_spec.year, date_spec.month, date_spec.day)
        elif date_spec.kind == "md":
            d = date(today.year, date_spec.month, date_spec.day)
            if d < today:
                d = date(today.year + 1, date_spec.month, date_spec.day)
        else:  # "d" : 이번 달 D일, 지났으면 다음 달 ("다음달 D일"은 항상 다음 달)
            ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            if date_spec.week_prefix in ("다음달", "담달"):
                d = date(ny, nm, date_spec.day)
            else:
                d = date(today.year, today.month, date_spec.day)
                if d < today and date_spec.week_prefix != "이번달":
                    d = date(ny, nm, date_spec.day)
    except TimeParseError:
        raise
    except ValueError:
        raise TimeParseError("존재하지 않는 날짜입니다. 월/일을 다시 확인해 주세요.") from None

    if check_weekday is not None and d.weekday() != check_weekday:
        names = "월화수목금토일"
        raise TimeParseError(
            f"{d.month}월 {d.day}일은 {names[d.weekday()]}요일입니다. 날짜와 요일을 다시 확인해 주세요."
        )
    return _clock_on_date(d, clock)


def parse_korean_time(text: str, base_time: datetime) -> Optional[datetime]:
    """기존 호출부 호환용: 실패 시 ``None`` 을 반환한다."""
    try:
        return parse_time(text, base_time)
    except TimeParseError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 6. 반복 예약 파서
# ─────────────────────────────────────────────────────────────────────────────
#
# parse_time() 은 1회성 시각만 다루고 반복 표현은 계속 거부한다(기존 API 유지).
# 반복 표현은 parse_recurrence() 가 별도의 결과 타입(Recurrence)으로 돌려준다.
# Recurrence.trigger_kwargs() 는 APScheduler CronTrigger / IntervalTrigger 에
# 그대로 넘길 수 있는 기본 타입(int/str)만 담은 dict 이다. 이 모듈은 APScheduler 에
# 의존하지 않는다.

MIN_INTERVAL_SECONDS = 10 * 60          # 'N분마다' 최소 간격 (알림 도배 방지)
MAX_INTERVAL_SECONDS = 7 * 24 * 60 * 60  # 'N시간마다' 최대 간격 (그 이상은 매일/매주 사용)

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_KO = "월화수목금토일"


@dataclass(frozen=True)
class Recurrence:
    """반복 규칙.

    - ``kind == "cron"``: ``cron`` 에 CronTrigger 인자 (예: ``{"day_of_week": "mon,wed,fri",
      "hour": 7, "minute": 0, "second": 0}``)
    - ``kind == "interval"``: ``interval_seconds`` 초 간격 (IntervalTrigger)
    - ``description``: 사용자에게 보여줄 한국어 설명 (예: "매주 월·수·금 오전 7시")
    """

    kind: str
    description: str
    cron: tuple = ()             # ((key, value), ...) — frozen 이라 dict 대신 tuple
    interval_seconds: int = 0

    def trigger_kwargs(self) -> dict:
        if self.kind == "interval":
            return {"seconds": self.interval_seconds}
        return dict(self.cron)


_REC_HELP = "예: '매일 오전 7시', '평일 8시 30분', '매주 월수금 9시', '매월 1일 오전 10시', '2시간마다'"

# 반복 표현에서 쓰는 요일 목록 ("월수금", "월,수,금", "월~금", "월요일부터 금요일까지")
_KO_DAY = r"[월화수목금토일](?:요일|욜)?"
_KO_ITEM = rf"{_KO_DAY}(?:(?:-|부터){_KO_DAY}(?:까지)?)?"
_KO_SEP = r"(?:/|·|・|&|\+|이랑|랑|하고|그리고|및|와|과)?"
_KO_LIST = rf"{_KO_ITEM}(?:{_KO_SEP}{_KO_ITEM})*"
_KO_ITEM_RE = re.compile(rf"(?P<a>[월화수목금토일])(?:요일|욜)?(?:(?:-|부터)(?P<b>[월화수목금토일])(?:요일|욜)?(?:까지)?)?")

_EN_DAY = "|".join(sorted(_EN_WEEKDAYS, key=len, reverse=True))
_EN_ITEM = rf"(?:{_EN_DAY})(?:(?:-|to|through|thru)(?:{_EN_DAY}))?"
_EN_LIST = rf"{_EN_ITEM}(?:(?:and|&|/|\+)?{_EN_ITEM})*"
_EN_ITEM_RE = re.compile(rf"(?P<a>{_EN_DAY})(?:(?:-|to|through|thru)(?P<b>{_EN_DAY}))?")

_REC_INTERVAL_KO_RE = re.compile(
    rf"(?P<dur>(?:{_NUM}(?:주일|주|일|시간|분|초)반?)+)(?:마다|간격으로|간격|에한번씩|에한번)"
)
_REC_INTERVAL_EN_RE = re.compile(rf"every(?P<dur>{_EN_DUR})")
_REC_HOURLY_RE = re.compile(
    r"매시간(?:마다)?(?:(?P<mi>\d{1,2})분)?|매시(?:(?P<mi2>\d{1,2})분|정각)|everyhour|hourly"
)
_REC_DAILY_RE = re.compile(r"매일매일|매일(?:마다)?|날마다|매날|everyday|daily|eachday")
_REC_WEEKDAYS_RE = re.compile(r"(?:매주)?(?:평일|주중)(?:마다)?|매평일|(?:every|each)?weekdays?")
_REC_WEEKEND_RE = re.compile(r"(?:매주)?주말(?:마다)?|(?:every|each)?weekends?")
_REC_MONTHLY_RE = re.compile(
    r"(?:매월|매달|달마다)(?:(?P<d>\d{1,2})일|(?P<last>말일|마지막날))(?:마다)?"
    r"|(?:everymonth|monthly)(?:on)?(?:the)?(?:(?P<d2>\d{1,2})(?:st|nd|rd|th)|(?P<last2>lastday))"
)
_REC_YEARLY_RE = re.compile(r"(?:매년|해마다)(?P<m>\d{1,2})월(?P<d>\d{1,2})일(?:마다)?")
_REC_WEEKLY_RE = re.compile(
    rf"(?:매주|주마다)(?P<ko>{_KO_LIST})(?:마다)?"
    rf"|(?<![\d매평주휴생내요금명])(?P<ko2>{_KO_LIST})마다"
    rf"|(?:every|each|weekly(?:on)?)(?P<en>{_EN_LIST})"
)
# 규칙 키워드만 있고 내용이 빠진 경우 (예: "매주 9시", "매월 9시")
_REC_BARE_RE = re.compile(r"매주|매월|매달|매년|해마다|everymonth|monthly|everyweek|weekly|every|마다")

# 1회성 '이번 주말' 같은 표현은 반복으로 보지 않는다.
_RECURRING_HINT_RE = re.compile(
    _RECURRING_RE.pattern + r"|(?<!이번)(?<!다음)(?<!담)(?<!지난)주말|평일|주중|weekdays?|weekends?|hourly|매시|해마다|날마다|간격|에한번"
)


def is_recurring(text: str) -> bool:
    """입력이 반복 예약 표현('매일', '매주', '~마다', 'every' 등)으로 보이면 True."""
    if text is None or not str(text).strip():
        return False
    return bool(_RECURRING_HINT_RE.search(_normalize(_pre_recurrence(str(text)))))


def _pre_recurrence(text: str) -> str:
    # '~' 는 _normalize 에서 지워지므로 요일 범위("월~금")를 위해 '-' 로 바꿔 둔다.
    s = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s*[~〜–—]\s*", "-", s)


def _take_clock(t: "_Text") -> Optional[_Clock]:
    """parse_time 의 시각 추출과 같은 규칙으로 시각 하나를 꺼낸다 (없으면 None)."""
    clocks: list = []
    for m in t.take_all(_SPECIAL_TIME_RE):
        w = m.group(0)
        if w in ("자정", "한밤중", "midnight"):
            clocks.append(_Clock(hour=0, special="midnight"))
        else:
            clocks.append(_Clock(hour=12, special="noon"))
    for regex in _TIME_RES:
        for m in t.take_all(regex):
            clocks.append(_parse_clock(m))
    lone_mer = t.take_all(_LONE_MERIDIEM_RE)

    if len(clocks) > 1:
        raise TimeParseError("시각이 여러 개 입력되었습니다. 하나만 입력해 주세요.")
    clock = clocks[0] if clocks else None
    if clock is not None:
        if lone_mer:
            if clock.meridiem or clock.special or len(lone_mer) > 1:
                raise TimeParseError("시간대 표현(오전/오후 등)이 중복되었습니다.")
            mer = lone_mer[0].group(0)
            clock.meridiem = _MER_ALIASES.get(mer, mer)
        _validate_clock(clock)
    elif lone_mer:
        raise TimeParseError(f"몇 시에 울릴지 함께 입력해 주세요. {_REC_HELP}")
    return clock


def _clock_to_hms(c: _Clock) -> tuple:
    """시각 → (시, 분, 초, 날짜이동). 날짜이동 1 = 다음날 (예: '밤 1시', '자정')."""
    ref = date(2026, 1, 5)  # 기준 날짜는 아무 날이나 무관
    dt_ = _clock_on_date(ref, c)
    return dt_.hour, dt_.minute, dt_.second, (dt_.date() - ref).days


def _fmt_clock(h: int, m: int, s: int) -> str:
    if (h, m, s) == (0, 0, 0):
        return "자정(0시)"
    if (h, m, s) == (12, 0, 0):
        return "정오(낮 12시)"
    ampm = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    out = f"{ampm} {h12}시"
    if m or s:
        out += f" {m}분"
    if s:
        out += f" {s}초"
    return out


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}시간")
    if m:
        parts.append(f"{m}분")
    if s:
        parts.append(f"{s}초")
    return " ".join(parts)


def _expand_days(items) -> set:
    """[(a, b), ...] (요일 번호, b 는 범위 끝 또는 None) → 요일 번호 집합."""
    out = set()
    for a, b in items:
        if b is None:
            out.add(a)
            continue
        i = a
        while True:  # '금~월' 처럼 주를 넘어가는 범위도 허용
            out.add(i)
            if i == b:
                break
            i = (i + 1) % 7
    return out


def _days_description(days: set) -> str:
    if days == set(range(7)):
        return "매일"
    if days == set(range(5)):
        return "평일(월~금)"
    if days == {5, 6}:
        return "주말(토·일)"
    if len(days) == 1:
        return f"매주 {_WEEKDAY_KO[next(iter(days))]}요일"
    return "매주 " + "·".join(_WEEKDAY_KO[d] for d in sorted(days))


def parse_recurrence(text: str, base_time: Optional[datetime] = None) -> Recurrence:
    """반복 예약 표현을 해석해 ``Recurrence`` 를 돌려준다.

    지원: 매일 / 평일 / 주말 / 매주 X요일(여러 요일·범위) / 매월 N일·말일 / 매년 M월 D일 /
    매시간 / N시간·N분마다 (최소 10분) 와 영어 표현(every day 7am, every 30 minutes …).
    모든 시각은 한국 시간 기준이다. ``base_time`` 은 현재 사용하지 않으며 API 대칭용이다.

    :raises TimeParseError: 반복 표현이 아니거나 해석할 수 없는 경우
    """
    if text is None or not str(text).strip():
        raise TimeParseError(f"반복 시간을 입력해 주세요. {_REC_HELP}")

    t = _Text(_normalize(_pre_recurrence(str(text))))

    rule = None  # (kind, payload)
    found = []

    def _found(kind, payload):
        found.append((kind, payload))

    # '매월 15일마다' 가 '15일마다'(간격)로 잘못 잡히지 않도록 월/연 단위를 먼저 꺼낸다.
    for m in t.take_all(_REC_MONTHLY_RE):
        d = m.group("d") or m.group("d2")
        _found("monthly", "last" if (m.group("last") or m.group("last2")) else int(d))
    for m in t.take_all(_REC_YEARLY_RE):
        _found("yearly", (int(m.group("m")), int(m.group("d"))))
    for m in t.take_all(_REC_INTERVAL_KO_RE):
        _found("interval_ko", m.group("dur"))
    for m in t.take_all(_REC_INTERVAL_EN_RE):
        _found("interval_en", m.group("dur"))
    for m in t.take_all(_REC_HOURLY_RE):
        mi = m.group("mi") or m.group("mi2")
        _found("hourly", int(mi) if mi else 0)
    for _ in t.take_all(_REC_DAILY_RE):
        _found("days", set(range(7)))
    for _ in t.take_all(_REC_WEEKDAYS_RE):
        _found("days", set(range(5)))
    for _ in t.take_all(_REC_WEEKEND_RE):
        _found("days", {5, 6})
    for m in t.take_all(_REC_WEEKLY_RE):
        if m.group("en"):
            items = [(_EN_WEEKDAYS[x["a"]], _EN_WEEKDAYS[x["b"]] if x["b"] else None)
                     for x in _EN_ITEM_RE.finditer(m.group("en"))]
        else:
            body = m.group("ko") or m.group("ko2")
            items = [(_KO_WEEKDAYS[x["a"]], _KO_WEEKDAYS[x["b"]] if x["b"] else None)
                     for x in _KO_ITEM_RE.finditer(body)]
        _found("days", _expand_days(items))

    if len(found) > 1:
        raise TimeParseError("반복 규칙이 여러 개 입력되었습니다. 하나만 입력해 주세요. " + _REC_HELP)
    if not found:
        bare = _REC_BARE_RE.search(t.s)
        if bare and bare.group(0) in ("매주", "everyweek", "weekly"):
            raise TimeParseError("'매주' 뒤에 요일을 함께 입력해 주세요. 예: '매주 월요일 9시', '매주 월수금 9시'")
        if bare and bare.group(0) in ("매월", "매달", "everymonth", "monthly"):
            raise TimeParseError("'매월' 뒤에 날짜를 함께 입력해 주세요. 예: '매월 1일 오전 9시', '매월 말일 오후 6시'")
        if bare and bare.group(0) in ("매년", "해마다"):
            raise TimeParseError("'매년' 뒤에 월/일을 함께 입력해 주세요. 예: '매년 12월 25일 오전 9시'")
        raise TimeParseError(f"반복 규칙을 이해하지 못했습니다. {_REC_HELP}")
    kind, payload = found[0]

    # ── 간격 반복 (N시간/N분마다) ─────────────────────────────────────────
    if kind in ("interval_ko", "interval_en"):
        korean = kind == "interval_ko"
        if re.search(r"주일|주|일" if korean else r"w|d", payload):
            try:
                dur = _parse_duration(payload, korean=korean)
            except OverflowError:
                raise TimeParseError("반복 간격이 너무 깁니다.") from None
            if dur == timedelta(days=1):
                kind, payload = "days", set(range(7))   # '하루마다' = 매일
            else:
                raise TimeParseError(
                    "일/주 단위 간격 반복은 지원하지 않습니다. '매일 오전 9시', '매주 월요일 9시'처럼 입력해 주세요."
                )
        else:
            try:
                dur = _parse_duration(payload, korean=korean)
            except OverflowError:
                raise TimeParseError("반복 간격이 너무 깁니다.") from None
            seconds = int(dur.total_seconds())
            if seconds < MIN_INTERVAL_SECONDS:
                raise TimeParseError(f"반복 간격은 최소 {MIN_INTERVAL_SECONDS // 60}분 이상이어야 합니다.")
            if seconds > MAX_INTERVAL_SECONDS:
                raise TimeParseError("반복 간격은 7일(168시간) 이하로 입력해 주세요. 더 긴 주기는 '매주 X요일'을 사용해 주세요.")
            if _take_clock(t) is not None:
                raise TimeParseError("'~마다' 간격 반복과 시각을 함께 쓸 수 없습니다. 둘 중 하나만 입력해 주세요.")
            _check_leftover(t)
            return Recurrence("interval", f"{_fmt_duration(seconds)}마다", interval_seconds=seconds)

    # ── 매시간 ───────────────────────────────────────────────────────────
    if kind == "hourly":
        if not 0 <= payload <= 59:
            raise TimeParseError(f"분은 0~59 사이여야 합니다. (입력: {payload}분)")
        if _take_clock(t) is not None:
            raise TimeParseError("'매시간'과 시각을 함께 쓸 수 없습니다. 예: '매시간', '매시 30분'")
        _check_leftover(t)
        desc = "매시간 정각" if payload == 0 else f"매시 {payload}분"
        return Recurrence("cron", desc, cron=(("minute", payload), ("second", 0)))

    # ── 시각이 필요한 규칙 ────────────────────────────────────────────────
    clock = _take_clock(t)
    _check_leftover(t)
    if clock is None:
        raise TimeParseError(f"몇 시에 울릴지 함께 입력해 주세요. {_REC_HELP}")
    h, mi, sec, shift = _clock_to_hms(clock)
    when = _fmt_clock(h, mi, sec)
    hms = (("hour", h), ("minute", mi), ("second", sec))

    if kind == "days":
        days = {(d + shift) % 7 for d in payload}
        prefix = _days_description(days)
        if days == set(range(7)):
            return Recurrence("cron", f"매일 {when}", cron=hms)
        dow = ",".join(_WEEKDAY_KEYS[d] for d in sorted(days))
        return Recurrence("cron", f"{prefix} {when}", cron=(("day_of_week", dow),) + hms)

    if shift:
        raise TimeParseError("자정/밤 늦은 시각은 날짜가 넘어가 모호합니다. '오전 0시', '새벽 1시'처럼 입력해 주세요.")

    if kind == "monthly":
        if payload == "last":
            return Recurrence("cron", f"매월 말일 {when}", cron=(("day", "last"),) + hms)
        if not 1 <= payload <= 31:
            raise TimeParseError(f"일은 1~31 사이여야 합니다. (입력: {payload}일)")
        note = " (해당 날짜가 없는 달은 건너뜀)" if payload > 28 else ""
        return Recurrence("cron", f"매월 {payload}일 {when}{note}", cron=(("day", payload),) + hms)

    # yearly
    month, day = payload
    try:
        date(2024, month, day)  # 윤년 기준으로 존재하는 날짜인지 확인 (2월 29일 허용)
    except ValueError:
        raise TimeParseError("존재하지 않는 날짜입니다. 월/일을 다시 확인해 주세요.") from None
    note = " (윤년에만)" if (month, day) == (2, 29) else ""
    return Recurrence("cron", f"매년 {month}월 {day}일 {when}{note}",
                      cron=(("month", month), ("day", day)) + hms)


def _check_leftover(t: "_Text") -> None:
    rest = re.sub(r"마다|씩", "", t.leftover()).strip()
    if rest:
        raise TimeParseError(f"이해할 수 없는 부분이 있습니다: '{rest}'. {_REC_HELP}")
