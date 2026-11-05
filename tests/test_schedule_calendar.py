"""schedule_calendar.py 테스트 (디스코드 연결 없이 mock 으로 검증)."""
import asyncio
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import schedule_calendar as sc  # noqa: E402
from time_parser import TimeParseError  # noqa: E402

NOW = datetime(2026, 11, 5, 15, 0)  # 목요일
G1, G2 = 1001, 2002
CH = 555


@pytest.fixture
def store(monkeypatch, tmp_path):
    s = sc.CalendarStore(str(tmp_path / "cal.db"))
    monkeypatch.setattr(sc, "_store", s)
    monkeypatch.setattr(sc, "now_seoul", lambda: NOW)
    yield s
    s.close()


def perms(manage=False, admin=False):
    return SimpleNamespace(manage_guild=manage, administrator=admin)


def make_it(guild_id=G1, channel_id=CH, user_id=10, manage=False, admin=False):
    it = MagicMock()
    it.guild_id = guild_id
    it.channel_id = channel_id
    it.user = SimpleNamespace(id=user_id, guild_permissions=perms(manage, admin))
    it.response.send_message = AsyncMock()
    it.response.is_done = MagicMock(return_value=False)
    it.followup.send = AsyncMock()
    return it


def sent(it):
    return it.response.send_message.await_args


def add(store, title="회의", start=datetime(2026, 11, 6, 15), end=None, guild=G1, creator=10,
        all_day=False, remind=10, desc=""):
    return store.add(guild, CH, creator, title, start, end, desc, all_day, remind, now=NOW)


# ─── 날짜 전용 파싱 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("내일", date(2026, 11, 6)),
    ("모레", date(2026, 11, 7)),
    ("12월 25일", date(2026, 12, 25)),
    ("2026-12-25", date(2026, 12, 25)),
    ("다음주 금요일", date(2026, 11, 13)),
    ("2028년 2월 29일", date(2028, 2, 29)),
])
def test_parse_date_only_ok(text, expected):
    assert sc.parse_date_only(text, NOW) == expected


@pytest.mark.parametrize("text", ["내일 오후 3시", "15:30", "30분 뒤"])
def test_parse_date_only_rejects_time(text):
    with pytest.raises(TimeParseError, match="날짜만"):
        sc.parse_date_only(text, NOW)


@pytest.mark.parametrize("text", ["", "   ", None, "회의", "2027년 2월 29일"])
def test_parse_date_only_invalid(text):
    with pytest.raises(TimeParseError):
        sc.parse_date_only(text, NOW)


# ─── 기간 계산 ─────────────────────────────────────────────────────────────
def test_period_today():
    assert sc.period_range("오늘", date(2026, 11, 5)) == (date(2026, 11, 5), date(2026, 11, 6))


@pytest.mark.parametrize("today,start", [
    (date(2026, 11, 5), date(2026, 11, 1)),   # 목 → 일요일 시작
    (date(2026, 11, 1), date(2026, 11, 1)),   # 일요일 당일
    (date(2026, 11, 7), date(2026, 11, 1)),   # 토요일
    (date(2026, 1, 2), date(2025, 12, 28)),   # 연도 경계
])
def test_period_week(today, start):
    assert sc.period_range("이번주", today) == (start, start + timedelta(days=7))


@pytest.mark.parametrize("today,expected", [
    (date(2026, 11, 5), (date(2026, 11, 1), date(2026, 12, 1))),
    (date(2028, 2, 10), (date(2028, 2, 1), date(2028, 3, 1))),
    (date(2026, 12, 31), (date(2026, 12, 1), date(2027, 1, 1))),
])
def test_period_this_month(today, expected):
    assert sc.period_range("이번달", today) == expected


@pytest.mark.parametrize("today,expected", [
    (date(2026, 12, 15), (date(2027, 1, 1), date(2027, 2, 1))),   # 연도 넘어감
    (date(2028, 1, 31), (date(2028, 2, 1), date(2028, 3, 1))),    # 윤년 2월(29일)
    (date(2027, 1, 31), (date(2027, 2, 1), date(2027, 3, 1))),    # 평년 2월
])
def test_period_next_month(today, expected):
    assert sc.period_range("다음달", today) == expected


def test_period_unknown():
    with pytest.raises(ValueError):
        sc.period_range("내년", date(2026, 1, 1))


@pytest.mark.parametrize("y,m,days", [(2024, 2, 29), (2100, 2, 28), (2000, 2, 29), (2026, 4, 30), (2026, 12, 31)])
def test_month_range_leap(y, m, days):
    s, e = sc.month_range(y, m)
    assert s == date(y, m, 1) and (e - s).days == days


# ─── 저장소: 기간 필터 / 서버 격리 ──────────────────────────────────────────
def test_list_range_filters_by_period(store):
    a = add(store, "이번주", datetime(2026, 11, 6, 10))
    b = add(store, "다음주", datetime(2026, 11, 9, 10))
    add(store, "지난달", datetime(2026, 10, 31, 23, 59))
    s, e = sc.period_range("이번주", NOW.date())
    assert [x.id for x in store.list_range(G1, s, e)] == [a.id]
    s, e = sc.period_range("이번달", NOW.date())
    assert [x.id for x in store.list_range(G1, s, e)] == [a.id, b.id]


def test_list_range_boundaries(store):
    first = add(store, "1일 0시", datetime(2026, 12, 1, 0, 0))
    last = add(store, "말일 23:59", datetime(2026, 11, 30, 23, 59))
    s, e = sc.month_range(2026, 11)
    assert [x.id for x in store.list_range(G1, s, e)] == [last.id]
    s, e = sc.month_range(2026, 12)
    assert [x.id for x in store.list_range(G1, s, e)] == [first.id]


def test_list_range_multi_day_overlap(store):
    ev = add(store, "연말휴가", datetime(2026, 12, 30), datetime(2027, 1, 2), all_day=True)
    assert store.list_range(G1, *sc.month_range(2027, 1))[0].id == ev.id
    assert store.list_range(G1, *sc.month_range(2026, 12))[0].id == ev.id
    assert store.list_range(G1, *sc.month_range(2027, 2)) == []


def test_event_ending_at_midnight_not_next_day(store):
    ev = add(store, "밤샘", datetime(2026, 11, 30, 22), datetime(2026, 12, 1, 0, 0))
    assert list(ev.days()) == [date(2026, 11, 30)]
    assert store.list_range(G1, *sc.month_range(2026, 12)) == []


def test_leap_day_event(store):
    ev = add(store, "윤일", datetime(2028, 2, 29), all_day=True)
    assert [x.id for x in store.list_range(G1, *sc.month_range(2028, 2))] == [ev.id]
    assert store.list_range(G1, *sc.month_range(2028, 3)) == []


def test_guild_isolation_store(store):
    ev = add(store, "G1 일정", guild=G1)
    add(store, "G2 일정", guild=G2)
    assert store.get(G2, ev.id) is None
    assert store.delete(G2, ev.id) is False
    assert store.update(G2, ev.id, title="해킹") is None
    assert store.get(G1, ev.id).title == "G1 일정"
    s, e = sc.month_range(2026, 11)
    assert [x.title for x in store.list_range(G1, s, e)] == ["G1 일정"]
    assert [x.title for x in store.list_range(G2, s, e)] == ["G2 일정"]


def test_update_rejects_protected_fields(store):
    ev = add(store)
    for f in ("id", "creator_id", "reminded", "nope"):
        with pytest.raises(ValueError):
            store.update(G1, ev.id, **{f: 1})


def test_db_path_env(monkeypatch, tmp_path):
    p = tmp_path / "env.db"
    monkeypatch.setenv("CALENDAR_DB_PATH", str(p))
    s = sc.CalendarStore()
    assert s.path == str(p) and p.exists()
    s.close()


def test_persistence_across_instances(tmp_path):
    p = str(tmp_path / "persist.db")
    s1 = sc.CalendarStore(p)
    ev = s1.add(G1, CH, 1, "유지", datetime(2026, 11, 6, 9), now=NOW)
    s1.close()
    s2 = sc.CalendarStore(p)
    assert s2.get(G1, ev.id).title == "유지"
    s2.close()


# ─── 달력 렌더링 ───────────────────────────────────────────────────────────
def test_render_month_text_layout():
    text = sc.render_month_text(2026, 11, [date(2026, 11, 6)], today=date(2026, 11, 5))
    lines = text.splitlines()
    assert lines[0] == "2026년 11월"
    assert lines[1].split() == list("일월화수목금토")
    assert lines[2] == "  1    2    3    4  [ 5]   6 *  7"  # 2026-11-01 은 일요일
    assert "[n] 오늘" in text


def test_render_month_text_today_with_event():
    text = sc.render_month_text(2026, 11, [date(2026, 11, 5)], today=date(2026, 11, 5))
    assert "[ 5]*" in text


def test_render_month_text_ignores_other_month_days():
    text = sc.render_month_text(2026, 11, [date(2026, 12, 6)], today=date(2026, 12, 1))
    assert "*" not in text.split("\n\n")[0] and "[" not in text.split("\n\n")[0]


def test_render_month_text_leap_feb():
    text = sc.render_month_text(2028, 2, [date(2028, 2, 29)])
    assert "29 *" in text
    assert " 30" not in text
    text2 = sc.render_month_text(2027, 2, [])
    assert "29" not in text2 and "28" in text2


def test_render_month_text_week_count():
    # 2026-08: 토요일 시작, 31일 → 6주
    body = sc.render_month_text(2026, 8, []).split("\n\n")[0].splitlines()[2:]
    assert len(body) == 6
    # 2026-02: 일요일 시작 28일 → 정확히 4주
    body = sc.render_month_text(2026, 2, []).split("\n\n")[0].splitlines()[2:]
    assert len(body) == 4


def test_render_month_text_cells_fixed_width():
    text = sc.render_month_text(2026, 11, [date(2026, 11, d) for d in range(1, 31)], today=date(2026, 11, 14))
    for line in text.splitlines()[2:7]:
        assert len(line) <= 35  # 7칸 × 5글자


def test_render_month_png_or_none(store):
    ev = add(store, "이미지 일정", datetime(2026, 11, 6, 10))
    png = sc.render_month_png(2026, 11, [ev], date(2026, 11, 5))
    if sc.find_korean_font() is None:
        assert png is None
    else:
        assert png.startswith(b"\x89PNG")


def test_render_month_png_no_font(monkeypatch):
    monkeypatch.setattr(sc, "find_korean_font", lambda: None)
    assert sc.render_month_png(2026, 11, [], None) is None


def test_render_month_png_bad_font(tmp_path):
    bad = tmp_path / "bad.ttf"
    bad.write_bytes(b"not a font")
    assert sc.render_month_png(2026, 11, [], None, font_path=str(bad)) is None


def test_find_font_env(monkeypatch, tmp_path):
    f = tmp_path / "x.ttf"
    f.write_bytes(b"")
    monkeypatch.setenv("CALENDAR_FONT_PATH", str(f))
    assert sc.find_korean_font() == str(f)


# ─── 임베드 / 길이 제한 ────────────────────────────────────────────────────
def test_list_embed_groups_by_day(store):
    add(store, "A", datetime(2026, 11, 6, 9))
    add(store, "B", datetime(2026, 11, 6, 14))
    add(store, "C", datetime(2026, 11, 7, 9))
    s, e = sc.period_range("이번주", NOW.date())
    emb = sc.build_list_embed("t", store.list_range(G1, s, e), s, e)
    assert [f.name for f in emb.fields] == ["2026년 11월 6일 (금)", "2026년 11월 7일 (토)"]
    assert "A" in emb.fields[0].value and "B" in emb.fields[0].value
    assert emb.footer.text == "총 3건"


def test_list_embed_empty():
    emb = sc.build_list_embed("t", [], date(2026, 11, 1), date(2026, 11, 2))
    assert "없습니다" in emb.description and not emb.fields


def test_list_embed_respects_limits(store):
    for d in range(1, 31):
        for i in range(12):
            add(store, "가" * 100, datetime(2026, 11, d, 8 + i))
    s, e = sc.month_range(2026, 11)
    emb = sc.build_list_embed("t" * 300, store.list_range(G1, s, e), s, e)
    assert len(emb.fields) <= sc.EMBED_MAX_FIELDS
    assert all(len(f.value) <= sc.EMBED_FIELD_VALUE_LIMIT for f in emb.fields)
    assert len(emb.title) <= sc.EMBED_TITLE_LIMIT
    assert len(emb) <= sc.EMBED_TOTAL_LIMIT
    assert "생략" in emb.footer.text


def test_month_summary_limit(store):
    evs = [add(store, "나" * 100, datetime(2026, 11, 1 + i % 30, 9)) for i in range(60)]
    text = sc.month_summary(evs, *sc.month_range(2026, 11), limit=1024)
    assert len(text) <= 1024 and "외" in text


def test_month_summary_multi_day_listed_once(store):
    ev = add(store, "출장", datetime(2026, 11, 3), datetime(2026, 11, 6), all_day=True)
    text = sc.month_summary([ev], *sc.month_range(2026, 11), limit=1024)
    assert text.count("출장") == 1


def test_clean_text_truncates_and_neutralizes_codeblock():
    assert len(sc.clean_text("a" * 500, sc.TITLE_MAX)) == sc.TITLE_MAX
    assert "```" not in sc.clean_text("x```y", 50)


# ─── 명령: 추가 ────────────────────────────────────────────────────────────
async def test_cmd_add_basic(store):
    it = make_it()
    await sc.cmd_add.callback(it, "회의", "내일 오후 3시")
    ev = store.get(G1, 1)
    assert ev.start == datetime(2026, 11, 6, 15) and ev.remind_minutes == 10 and ev.creator_id == 10
    kw = sent(it).kwargs
    assert kw["allowed_mentions"].everyone is False and kw["allowed_mentions"].roles is False
    assert kw["allowed_mentions"].users is False


async def test_cmd_add_with_end_and_desc(store):
    it = make_it()
    await sc.cmd_add.callback(it, "워크숍", "다음주 금요일 오후 1시", "오후 5시", "장소: 3층", False, 30)
    ev = store.get(G1, 1)
    assert ev.start == datetime(2026, 11, 13, 13) and ev.end == datetime(2026, 11, 13, 17)
    assert ev.description == "장소: 3층" and ev.remind_minutes == 30


async def test_cmd_add_all_day(store):
    it = make_it()
    await sc.cmd_add.callback(it, "크리스마스", "12월 25일", None, None, True)
    ev = store.get(G1, 1)
    assert ev.all_day and ev.start == datetime(2026, 12, 25)
    assert store.remind_at(ev.id) == datetime(2026, 12, 25, 8, 50)


async def test_cmd_add_all_day_rejects_time(store):
    it = make_it()
    await sc.cmd_add.callback(it, "x", "내일 오후 3시", None, None, True)
    assert sent(it).kwargs["ephemeral"] is True and "날짜만" in sent(it).args[0]
    assert store.get(G1, 1) is None


async def test_cmd_add_no_reminder(store):
    it = make_it()
    await sc.cmd_add.callback(it, "x", "내일 오후 3시", 알림분=-1)
    assert store.get(G1, 1).remind_minutes is None
    assert store.due_reminders(datetime(2030, 1, 1)) == []


async def test_cmd_add_bad_time_is_ephemeral(store):
    it = make_it()
    await sc.cmd_add.callback(it, "x", "언젠가")
    assert sent(it).kwargs["ephemeral"] is True and store.get(G1, 1) is None


@pytest.mark.parametrize("start,end,expected", [
    ("다음주 금요일 오후 1시", "오후 5시", datetime(2026, 11, 13, 17)),      # 시각만 → 시작일 기준
    ("내일 오후 3시", "2시간 뒤", datetime(2026, 11, 6, 17)),               # 기간 → 시작 기준
    ("내일 오후 3시", "모레 오전 10시", datetime(2026, 11, 7, 10)),          # 날짜 포함 → 현재 기준
    ("12월 24일 오후 6시", "12월 25일 오전 2시", datetime(2026, 12, 25, 2)),
])
async def test_cmd_add_end_resolution(store, start, end, expected):
    await sc.cmd_add.callback(make_it(), "x", start, end)
    assert store.get(G1, 1).end == expected


async def test_cmd_add_end_before_start(store):
    it = make_it()
    await sc.cmd_add.callback(it, "x", "내일 오후 3시", "내일 오후 1시")
    assert sent(it).kwargs["ephemeral"] is True and "앞설" in sent(it).args[0]


async def test_cmd_add_empty_title(store):
    it = make_it()
    await sc.cmd_add.callback(it, "   ", "내일 오후 3시")
    assert sent(it).kwargs["ephemeral"] is True


async def test_cmd_add_long_title_truncated(store):
    it = make_it()
    await sc.cmd_add.callback(it, "제" * 500, "내일 오후 3시", None, "설" * 5000)
    ev = store.get(G1, 1)
    assert len(ev.title) == sc.TITLE_MAX and len(ev.description) == sc.DESCRIPTION_MAX
    emb = sent(it).kwargs["embed"]
    assert len(emb) <= sc.EMBED_TOTAL_LIMIT and len(emb.title) <= sc.EMBED_TITLE_LIMIT


async def test_cmd_add_in_dm_rejected(store):
    it = make_it(guild_id=None)
    await sc.cmd_add.callback(it, "x", "내일 오후 3시")
    assert sent(it).kwargs["ephemeral"] is True and store.get(G1, 1) is None


async def test_error_uses_followup_when_done(store):
    it = make_it(guild_id=None)
    it.response.is_done.return_value = True
    await sc.cmd_add.callback(it, "x", "내일 오후 3시")
    assert it.followup.send.await_args.kwargs["ephemeral"] is True


# ─── 명령: 목록 / 달력 ─────────────────────────────────────────────────────
async def test_cmd_list_period_and_isolation(store):
    add(store, "우리 서버", datetime(2026, 11, 5, 18), guild=G1)
    add(store, "남의 서버", datetime(2026, 11, 5, 18), guild=G2)
    add(store, "내일", datetime(2026, 11, 6, 18), guild=G1)
    it = make_it()
    await sc.cmd_list.callback(it, "오늘")
    emb = sent(it).kwargs["embed"]
    vals = " ".join(f.value for f in emb.fields)
    assert "우리 서버" in vals and "남의 서버" not in vals and "내일" not in vals
    assert sent(it).kwargs["allowed_mentions"].everyone is False


async def test_cmd_list_default_week(store):
    add(store, "토요일", datetime(2026, 11, 7, 10))
    add(store, "다음 일요일", datetime(2026, 11, 8, 10))
    it = make_it()
    await sc.cmd_list.callback(it)
    vals = " ".join(f.value for f in sent(it).kwargs["embed"].fields)
    assert "토요일" in vals and "다음 일요일" not in vals


async def test_cmd_list_next_month(store):
    add(store, "12월", datetime(2026, 12, 31, 10))
    it = make_it()
    await sc.cmd_list.callback(it, "다음달")
    assert "12월" in sent(it).kwargs["embed"].fields[0].value


async def test_cmd_calendar_text(store):
    add(store, "행사", datetime(2026, 11, 20, 10))
    add(store, "남의 행사", datetime(2026, 11, 21, 10), guild=G2)
    it = make_it()
    await sc.cmd_calendar.callback(it, None, None, False)
    emb = sent(it).kwargs["embed"]
    assert emb.title == "📆 2026년 11월"
    assert "```" in emb.description and "[ 5]" in emb.description and "20 *" in emb.description
    assert "21 *" not in emb.description
    assert "행사" in emb.fields[0].value and "남의" not in emb.fields[0].value
    assert "file" not in sent(it).kwargs


async def test_cmd_calendar_specific_month(store):
    it = make_it()
    await sc.cmd_calendar.callback(it, 2028, 2, False)
    emb = sent(it).kwargs["embed"]
    assert emb.title == "📆 2028년 2월" and "29" in emb.description and "[" not in emb.description.split("[n]")[0]


async def test_cmd_calendar_image_fallback(store, monkeypatch):
    monkeypatch.setattr(sc, "find_korean_font", lambda: None)
    it = make_it()
    await sc.cmd_calendar.callback(it, None, None, True)
    emb = sent(it).kwargs["embed"]
    assert "텍스트 달력" in emb.description and "```" in emb.description
    assert "file" not in sent(it).kwargs


async def test_cmd_calendar_image(store, monkeypatch):
    monkeypatch.setattr(sc, "render_month_png", lambda *a, **k: b"\x89PNGfake")
    it = make_it()
    await sc.cmd_calendar.callback(it, None, None, True)
    kw = sent(it).kwargs
    assert isinstance(kw["file"], discord.File) and kw["embed"].image.url == "attachment://calendar.png"


# ─── 명령: 수정 / 삭제 권한 ─────────────────────────────────────────────────
async def test_edit_by_creator(store):
    ev = add(store, creator=10)
    it = make_it(user_id=10)
    await sc.cmd_edit.callback(it, ev.id, "새 제목")
    assert store.get(G1, ev.id).title == "새 제목"
    assert "ephemeral" not in sent(it).kwargs


async def test_edit_by_other_denied(store):
    ev = add(store, creator=10)
    it = make_it(user_id=99)
    await sc.cmd_edit.callback(it, ev.id, "해킹")
    assert sent(it).kwargs["ephemeral"] is True and store.get(G1, ev.id).title == "회의"


@pytest.mark.parametrize("manage,admin", [(True, False), (False, True)])
async def test_edit_by_manager(store, manage, admin):
    ev = add(store, creator=10)
    it = make_it(user_id=99, manage=manage, admin=admin)
    await sc.cmd_edit.callback(it, ev.id, "관리자 수정")
    assert store.get(G1, ev.id).title == "관리자 수정"


async def test_edit_other_guild_not_found(store):
    ev = add(store, guild=G1, creator=10)
    it = make_it(guild_id=G2, user_id=10, admin=True)
    await sc.cmd_edit.callback(it, ev.id, "x")
    assert sent(it).kwargs["ephemeral"] is True and store.get(G1, ev.id).title == "회의"


async def test_edit_nothing(store):
    ev = add(store)
    it = make_it()
    await sc.cmd_edit.callback(it, ev.id)
    assert sent(it).kwargs["ephemeral"] is True


async def test_edit_time_resets_reminder(store):
    ev = add(store, start=datetime(2026, 11, 5, 15, 5))
    assert store.claim_reminder(ev.id)
    it = make_it()
    await sc.cmd_edit.callback(it, ev.id, None, "내일 오후 3시")
    new = store.get(G1, ev.id)
    assert new.start == datetime(2026, 11, 6, 15) and new.reminded is False
    assert store.remind_at(ev.id) == datetime(2026, 11, 6, 14, 50)


async def test_edit_title_keeps_reminded_flag(store):
    ev = add(store)
    store.claim_reminder(ev.id)
    await sc.cmd_edit.callback(make_it(), ev.id, "제목만")
    assert store.get(G1, ev.id).reminded is True


async def test_edit_to_all_day_and_clear_end(store):
    ev = add(store, start=datetime(2026, 11, 6, 15), end=datetime(2026, 11, 6, 16))
    await sc.cmd_edit.callback(make_it(), ev.id, None, None, "없음", "없음", True)
    new = store.get(G1, ev.id)
    assert new.all_day and new.start == datetime(2026, 11, 6) and new.end is None and new.description == ""


async def test_edit_bad_time(store):
    ev = add(store)
    it = make_it()
    await sc.cmd_edit.callback(it, ev.id, None, "아무때나")
    assert sent(it).kwargs["ephemeral"] is True and store.get(G1, ev.id).start == ev.start


async def test_edit_end_before_start(store):
    ev = add(store, start=datetime(2026, 11, 6, 15))
    it = make_it()
    await sc.cmd_edit.callback(it, ev.id, None, None, "내일 오후 1시")
    assert sent(it).kwargs["ephemeral"] is True


async def test_edit_remind_minutes(store):
    ev = add(store)
    await sc.cmd_edit.callback(make_it(), ev.id, None, None, None, None, None, 60)
    assert store.remind_at(ev.id) == datetime(2026, 11, 6, 14)
    await sc.cmd_edit.callback(make_it(), ev.id, None, None, None, None, None, -1)
    assert store.remind_at(ev.id) is None


async def test_delete_by_creator(store):
    ev = add(store, creator=10)
    it = make_it(user_id=10)
    await sc.cmd_delete.callback(it, ev.id)
    assert store.get(G1, ev.id) is None
    assert sent(it).kwargs["allowed_mentions"].everyone is False


async def test_delete_by_other_denied(store):
    ev = add(store, creator=10)
    it = make_it(user_id=99)
    await sc.cmd_delete.callback(it, ev.id)
    assert store.get(G1, ev.id) is not None and sent(it).kwargs["ephemeral"] is True


async def test_delete_by_manager(store):
    ev = add(store, creator=10)
    await sc.cmd_delete.callback(make_it(user_id=99, manage=True), ev.id)
    assert store.get(G1, ev.id) is None


async def test_delete_other_guild(store):
    ev = add(store, guild=G1)
    it = make_it(guild_id=G2, admin=True)
    await sc.cmd_delete.callback(it, ev.id)
    assert store.get(G1, ev.id) is not None and sent(it).kwargs["ephemeral"] is True


async def test_delete_missing(store):
    it = make_it()
    await sc.cmd_delete.callback(it, 999)
    assert sent(it).kwargs["ephemeral"] is True


def test_can_modify_without_permissions_attr():
    it = MagicMock()
    it.user = SimpleNamespace(id=5)
    ev = SimpleNamespace(creator_id=6)
    assert sc.can_modify(it, ev) is False


# ─── 알림 / 중복 방지 ──────────────────────────────────────────────────────
def make_bot():
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot, channel


async def test_reminder_sent_once(store):
    add(store, "@everyone 회의", datetime(2026, 11, 5, 15, 10), remind=10)
    bot, ch = make_bot()
    assert await sc.process_reminders(bot, store, datetime(2026, 11, 5, 14, 59)) == 0
    assert await sc.process_reminders(bot, store, datetime(2026, 11, 5, 15, 0)) == 1
    assert await sc.process_reminders(bot, store, datetime(2026, 11, 5, 15, 1)) == 0
    assert ch.send.await_count == 1
    am = ch.send.await_args.kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users is False
    assert "10분 뒤" in ch.send.await_args.args[0]


async def test_reminder_dedup_concurrent(store):
    add(store, start=datetime(2026, 11, 5, 15, 10))
    bot, ch = make_bot()
    now = datetime(2026, 11, 5, 15, 0)
    results = await asyncio.gather(*(sc.process_reminders(bot, store, now) for _ in range(5)))
    assert sum(results) == 1 and ch.send.await_count == 1


async def test_reminder_flag_persisted_across_store_instances(tmp_path):
    p = str(tmp_path / "r.db")
    s1 = sc.CalendarStore(p)
    s1.add(G1, CH, 1, "x", datetime(2026, 11, 5, 15, 10), now=NOW)
    bot, ch = make_bot()
    await sc.process_reminders(bot, s1, datetime(2026, 11, 5, 15, 0))
    s1.close()
    s2 = sc.CalendarStore(p)  # 봇 재시작 상황
    assert await sc.process_reminders(bot, s2, datetime(2026, 11, 5, 15, 1)) == 0
    assert ch.send.await_count == 1
    s2.close()


async def test_stale_reminder_skipped_and_marked(store):
    ev = add(store, start=datetime(2026, 11, 5, 12, 0))   # 알림 11:50, 지금 15:00
    bot, ch = make_bot()
    assert await sc.process_reminders(bot, store, NOW) == 0
    assert ch.send.await_count == 0 and store.get(G1, ev.id).reminded is True


async def test_reminder_within_grace_sent(store):
    add(store, start=datetime(2026, 11, 5, 15, 0))   # 알림 14:50, 지금 15:00 → 10분 이내
    bot, ch = make_bot()
    assert await sc.process_reminders(bot, store, NOW) == 1


async def test_reminder_custom_minutes(store):
    add(store, start=datetime(2026, 11, 5, 16, 0), remind=60)
    bot, ch = make_bot()
    assert await sc.process_reminders(bot, store, datetime(2026, 11, 5, 14, 59)) == 0
    assert await sc.process_reminders(bot, store, datetime(2026, 11, 5, 15, 0)) == 1


async def test_reminder_zero_minutes(store):
    add(store, start=datetime(2026, 11, 5, 15, 0), remind=0)
    bot, ch = make_bot()
    assert await sc.process_reminders(bot, store, NOW) == 1
    assert "지금 시작" in ch.send.await_args.args[0]


async def test_all_day_reminder(store):
    add(store, "휴일", datetime(2026, 11, 6), all_day=True, remind=0)
    bot, ch = make_bot()
    assert await sc.process_reminders(bot, store, datetime(2026, 11, 6, 8, 59)) == 0
    assert await sc.process_reminders(bot, store, datetime(2026, 11, 6, 9, 0)) == 1
    assert "종일" in ch.send.await_args.args[0]


async def test_reminder_fetch_channel_fallback(store):
    add(store, start=datetime(2026, 11, 5, 15, 10))
    bot, ch = make_bot()
    bot.get_channel.return_value = None
    assert await sc.process_reminders(bot, store, NOW) == 1
    bot.fetch_channel.assert_awaited_once_with(CH)


async def test_reminder_send_failure_not_retried(store, capsys):
    add(store, start=datetime(2026, 11, 5, 15, 10))
    bot, ch = make_bot()
    ch.send.side_effect = RuntimeError("boom")
    assert await sc.process_reminders(bot, store, NOW) == 0
    assert await sc.process_reminders(bot, store, NOW) == 0
    assert ch.send.await_count == 1
    assert "전송 실패" in capsys.readouterr().out


async def test_reminder_failure_does_not_block_others(store):
    add(store, "a", datetime(2026, 11, 5, 15, 10))
    add(store, "b", datetime(2026, 11, 5, 15, 10))
    bot, ch = make_bot()
    ch.send.side_effect = [RuntimeError("x"), None]
    assert await sc.process_reminders(bot, store, NOW) == 1


def test_reminder_text_length():
    ev = sc.Event(1, G1, CH, 1, "t" * 100, "d" * 1000, datetime(2026, 11, 5, 15), None, False, 10, False)
    assert len(sc.reminder_text(ev)) <= sc.MESSAGE_LIMIT


async def test_reminder_loop_runs_and_survives_errors(monkeypatch):
    bot = MagicMock()
    states = iter([False, False, True])
    bot.is_closed = lambda: next(states)
    calls = []

    async def fake_process(b, s, now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("db locked")
        return 0

    monkeypatch.setattr(sc, "process_reminders", fake_process)
    await sc.reminder_loop(bot, store_getter=lambda: None, interval=0)
    assert len(calls) == 2


# ─── 등록 ─────────────────────────────────────────────────────────────────
async def test_register_adds_commands_and_single_loop(monkeypatch):
    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    sc.register(bot)
    names = {c.name for c in bot.tree.get_commands()}
    assert {"일정추가", "일정목록", "달력", "일정수정", "일정삭제"} <= names

    started = []

    async def fake_loop(b, *a, **k):
        started.append(b)
        await asyncio.sleep(3600)

    monkeypatch.setattr(sc, "reminder_loop", fake_loop)
    listener = bot.extra_events["on_ready"][0]
    await listener()
    await listener()  # 재연결로 on_ready 가 다시 와도 루프는 하나
    await asyncio.sleep(0)
    assert len(started) == 1
    bot._calendar_reminder_task.cancel()


def test_bot_registers_calendar():
    import bot as botmod
    names = {c.name for c in botmod.bot.tree.get_commands()}
    assert {"일정추가", "달력", "예약"} <= names


def test_module_only_exports_register():
    assert sc.__all__ == ["register"]


# ─── PNG 달력 (기본 출력) ──────────────────────────────────────────────────
from PIL import Image  # noqa: E402
import io  # noqa: E402

REPO_FONT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts", "NanumGothic-Regular.ttf")


def test_repo_font_and_license_present():
    assert os.path.isfile(REPO_FONT)
    assert os.path.isfile(os.path.join(os.path.dirname(REPO_FONT), "OFL.txt"))
    assert sc.find_korean_font() == REPO_FONT  # 저장소 폰트가 최우선


@pytest.mark.parametrize("y,m,weeks", [(2026, 11, 5), (2026, 8, 6), (2026, 2, 4), (2028, 2, 5)])
def test_png_size_matches_weeks(y, m, weeks):
    png = sc.render_month_png(y, m, [], None, font_path=REPO_FONT)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.size == sc.png_size(y, m)
    assert img.size == (sc.PNG_PAD * 2 + sc.PNG_CELL_W * 7,
                        sc.PNG_PAD * 2 + sc.PNG_TITLE_H + sc.PNG_HEAD_H + sc.PNG_CELL_H * weeks)


def _ev(i, start, all_day=False, end=None, title=None):
    return sc.Event(i, G1, CH, 1, title or f"일정{i}", "", start, end, all_day, 10, False)


def test_cell_labels_fit_and_overflow():
    d = date(2026, 11, 6)
    evs = [_ev(i, datetime(2026, 11, 6, 9 + i)) for i in range(sc.PNG_MAX_LINES)]
    labels = sc.cell_labels(evs, d)
    assert len(labels) == sc.PNG_MAX_LINES and labels[0] == "09:00 일정0"
    evs = [_ev(i, datetime(2026, 11, 6, 9, i)) for i in range(12)]
    labels = sc.cell_labels(evs, d)
    assert len(labels) == sc.PNG_MAX_LINES
    assert labels[-1] == f"+{12 - (sc.PNG_MAX_LINES - 1)}개"


def test_cell_labels_all_day_and_continued():
    ev = _ev(1, datetime(2026, 11, 5), all_day=True, end=datetime(2026, 11, 7))
    assert sc.cell_labels([ev], date(2026, 11, 6)) == ["종일 일정1"]
    ev2 = _ev(2, datetime(2026, 11, 5, 22), end=datetime(2026, 11, 6, 2))
    assert sc.cell_labels([ev2], date(2026, 11, 6)) == ["계속 일정2"]


def _cell_pixel(y, m, d, dx=60, dy=60):
    weeks = __import__("calendar").Calendar(firstweekday=6).monthdayscalendar(y, m)
    for r, week in enumerate(weeks):
        if d in week:
            c = week.index(d)
            return (sc.PNG_PAD + c * sc.PNG_CELL_W + dx, sc.PNG_PAD + sc.PNG_TITLE_H + sc.PNG_HEAD_H + r * sc.PNG_CELL_H + dy)


def test_png_today_highlight_and_busy_day():
    evs = [_ev(i, datetime(2026, 11, 20, 8, i), title="아주아주아주 긴 일정 제목입니다 정말로") for i in range(30)]
    png = sc.render_month_png(2026, 11, evs, date(2026, 11, 5), font_path=REPO_FONT)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert img.getpixel(_cell_pixel(2026, 11, 5, dx=150, dy=120)) == sc.COLOR_TODAY_BG
    assert img.getpixel(_cell_pixel(2026, 11, 6, dx=150, dy=120)) == sc.COLOR_BG
    # 일정 많은 날은 칸 안에 글자가 그려짐(배경색이 아닌 픽셀 존재), 칸 밖으로 넘치지 않음
    x0, y0 = _cell_pixel(2026, 11, 20, 0, 0)
    region = img.crop((x0 + 2, y0 + sc.PNG_DAY_H, x0 + sc.PNG_CELL_W - 2, y0 + sc.PNG_CELL_H - 2))
    assert any(p != sc.COLOR_BG for p in region.getdata())
    nx, ny = _cell_pixel(2026, 11, 27, 0, 0)  # 바로 아래 칸은 비어 있어야 함
    below = img.crop((nx + 3, ny + sc.PNG_DAY_H, nx + sc.PNG_CELL_W - 3, ny + sc.PNG_CELL_H - 3))
    assert all(p == sc.COLOR_BG for p in below.getdata())


def test_png_month_boundary_event_only_in_range():
    ev = _ev(1, datetime(2026, 10, 30), all_day=True, end=datetime(2026, 11, 2))
    png_nov = sc.render_month_png(2026, 11, [ev], None, font_path=REPO_FONT)
    img = Image.open(io.BytesIO(png_nov)).convert("RGB")
    x0, y0 = _cell_pixel(2026, 11, 3, 0, 0)  # 11/3 은 일정 없음
    assert all(p == sc.COLOR_BG for p in img.crop((x0 + 3, y0 + sc.PNG_DAY_H, x0 + sc.PNG_CELL_W - 3, y0 + sc.PNG_CELL_H - 3)).getdata())
    x1, y1 = _cell_pixel(2026, 11, 2, 0, 0)  # 11/2 은 일정 있음
    assert any(p != sc.COLOR_BG for p in img.crop((x1 + 3, y1 + sc.PNG_DAY_H, x1 + sc.PNG_CELL_W - 3, y1 + sc.PNG_CELL_H - 3)).getdata())


def test_png_weekend_header_colors():
    img = Image.open(io.BytesIO(sc.render_month_png(2026, 11, [], None, font_path=REPO_FONT))).convert("RGB")
    head = img.crop((sc.PNG_PAD, sc.PNG_PAD + sc.PNG_TITLE_H, sc.PNG_PAD + sc.PNG_CELL_W, sc.PNG_PAD + sc.PNG_TITLE_H + sc.PNG_HEAD_H))
    assert sc.COLOR_SUN in set(head.getdata())
    sat = img.crop((sc.PNG_PAD + 6 * sc.PNG_CELL_W, sc.PNG_PAD + sc.PNG_TITLE_H, sc.PNG_PAD + 7 * sc.PNG_CELL_W, sc.PNG_PAD + sc.PNG_TITLE_H + sc.PNG_HEAD_H))
    assert sc.COLOR_SAT in set(sat.getdata())


async def test_cmd_calendar_default_is_image_with_view(store):
    add(store, "팀 회의", datetime(2026, 11, 6, 15))
    it = make_it()
    await sc.cmd_calendar.callback(it)
    kw = sent(it).kwargs
    assert isinstance(kw["file"], discord.File) and kw["embed"].image.url == "attachment://calendar.png"
    assert isinstance(kw["view"], sc.CalendarView)
    assert "팀 회의" in kw["embed"].fields[0].value


@pytest.mark.parametrize("y,m,delta,expected", [
    (2026, 12, 1, (2027, 1)), (2026, 1, -1, (2025, 12)), (2026, 11, 0, (2026, 11)), (2026, 11, 14, (2028, 1)),
])
def test_shift_month(y, m, delta, expected):
    assert sc.shift_month(y, m, delta) == expected


async def test_calendar_view_navigation(store):
    add(store, "1월 일정", datetime(2027, 1, 10, 9))
    view = sc.CalendarView(G1, 2026, 12, image=False)
    it = make_it()
    it.response.edit_message = AsyncMock()
    await view._go(it, 1)
    kw = it.response.edit_message.await_args.kwargs
    assert (view.year, view.month) == (2027, 1)
    assert kw["embed"].title == "📆 2027년 1월" and "1월 일정" in kw["embed"].fields[0].value
    assert kw["attachments"] == [] and kw["view"] is view
    await view._go(it, -2)
    assert (view.year, view.month) == (2026, 11)


async def test_calendar_view_image_attachment(store):
    view = sc.CalendarView(G1, 2026, 11, image=True)
    it = make_it()
    it.response.edit_message = AsyncMock()
    await view._go(it, 1)
    att = it.response.edit_message.await_args.kwargs["attachments"]
    assert len(att) == 1 and isinstance(att[0], discord.File)


async def test_calendar_view_other_guild_blocked(store):
    add(store, "비밀", datetime(2026, 12, 1, 9), guild=G1)
    view = sc.CalendarView(G1, 2026, 11, image=False)
    it = make_it(guild_id=G2)
    it.response.edit_message = AsyncMock()
    await view._go(it, 1)
    it.response.edit_message.assert_not_awaited()
    assert sent(it).kwargs["ephemeral"] is True


async def test_calendar_view_range_limit(store):
    view = sc.CalendarView(G1, 2200, 12, image=False)
    it = make_it()
    it.response.edit_message = AsyncMock()
    await view._go(it, 1)
    it.response.edit_message.assert_not_awaited()


# ─── 일괄 등록 / 내보내기 ──────────────────────────────────────────────────
EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "일정_예시.txt")


def test_bulk_parse_pipe_format():
    text = "# 주석\n\n2026-11-20 14:00 | 팀 회의 | 3층 회의실 | 30\n2026-11-25 | 창립기념일\n11/27 19:30 | 회식\n"
    items, errors = sc.parse_bulk_text(text, NOW)
    assert errors == []
    a, b, c = items
    assert (a.line_no, a.start, a.all_day, a.title, a.description, a.remind_minutes) == \
        (3, datetime(2026, 11, 20, 14), False, "팀 회의", "3층 회의실", 30)
    assert b.all_day and b.start == datetime(2026, 11, 25) and b.remind_minutes == 10
    assert c.start == datetime(2026, 11, 27, 19, 30)


@pytest.mark.parametrize("line,expected", [
    ("11/5 14:00 | 지난 시각", datetime(2027, 11, 5, 14)),    # 오늘이지만 이미 지남 → 내년
    ("11/5 16:00 | 오늘 나중", datetime(2026, 11, 5, 16)),
    ("11/5 | 오늘 종일", datetime(2026, 11, 5)),              # 종일은 오늘 포함
    ("1/3 | 새해", datetime(2027, 1, 3)),
    ("2/29 | 윤일", datetime(2028, 2, 29)),                   # 다음 윤년
    ("2026.12.25 | 점 구분", datetime(2026, 12, 25)),
    ("2026/12/25 9:05 | 슬래시", datetime(2026, 12, 25, 9, 5)),
])
def test_bulk_year_inference(line, expected):
    items, errors = sc.parse_bulk_text(line, NOW)
    assert errors == [] and items[0].start == expected


@pytest.mark.parametrize("line,reason", [
    ("2026-13-01 | x", "존재하지 않는 날짜"),
    ("2027-02-29 | x", "존재하지 않는 날짜"),
    ("2026-11-20 25:00 | x", "시각 형식"),
    ("2026-11-20 3시간 | x", "시각 형식"),
    ("내일 | x", "날짜 형식"),
    ("2026-11-20", "'|'"),
    ("2026-11-20 |   ", "제목이 비어"),
    ("2026-11-20 | x | y | 알림많이", "알림 값"),
    ("2026-11-20 | x | y | 99999", "알림 값"),
    ("2026-11-20 | a | b | 1 | 초과", "칸이 너무"),
    ("2026-11-20 10:00 extra | x", "시각 형식"),
    ("11월 32일 | x", "존재하지 않는 날짜 '11월 32일'"),
    ("2026년 2월 30일 | x", "존재하지 않는 날짜"),
    ("11월 20일 오후 13시 | x", "시각 형식"),
    ("11월 20일 오전 0시 | x", "시각 형식"),
    ("11월 20일 오후 | x", "시각 형식"),
    ("11월 | x", "날짜 형식 오류 '11월'"),
])
def test_bulk_error_lines(line, reason):
    items, errors = sc.parse_bulk_text("2026-11-30 | 정상\n" + line, NOW)
    assert len(items) == 1 and len(errors) == 1
    assert errors[0][0] == 2 and reason in errors[0][1]


@pytest.mark.parametrize("val,expected", [("30", 30), ("30분전", 30), ("알림30분전", 30), ("0", 0), ("-1", None), ("", 10)])
def test_bulk_remind_values(val, expected):
    items, errors = sc.parse_bulk_text(f"2026-11-20 10:00 | x | | {val}", NOW)
    assert errors == [] and items[0].remind_minutes == expected


def test_bulk_csv_format():
    text = "날짜,시각,제목,설명,알림\n2026-11-20,14:00,팀 회의,\"3층, 큰 회의실\",30\n2026-11-25,,창립기념일,,\n,,빈날짜,,\n"
    items, errors = sc.parse_bulk_text(text, NOW)
    assert [i.title for i in items] == ["팀 회의", "창립기념일"]
    assert items[0].description == "3층, 큰 회의실" and items[1].all_day
    assert errors[0][0] == 4


def test_bulk_crlf_and_bom():
    data = "﻿2026-11-20 14:00 | 회의\r\n2026-11-21 | 휴가\r\n".encode("utf-8")
    items, errors = sc.parse_bulk_text(sc.decode_bulk_bytes(data), NOW)
    assert [i.title for i in items] == ["회의", "휴가"] and errors == []


@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "cp949"])
def test_bulk_decode_encodings(enc):
    text = "2026-11-20 | 한글 제목 테스트\n"
    assert sc.decode_bulk_bytes(text.encode(enc)).lstrip("﻿") == text


def test_bulk_decode_unknown():
    with pytest.raises(sc.BulkParseError):
        sc.decode_bulk_bytes(b"\xff\xfe\xff\xfe\x80\x81\xff")


def test_bulk_line_limit():
    ok = "\n".join(f"2026-12-01 | 일정{i}" for i in range(1000))
    items, errors = sc.parse_bulk_text("# 주석은 세지 않음\n\n" + ok, NOW)
    assert len(items) == 1000 and errors == []
    with pytest.raises(sc.BulkParseError, match="1000"):
        sc.parse_bulk_text(ok + "\n2026-12-02 | 초과", NOW)


def test_bulk_empty():
    with pytest.raises(sc.BulkParseError):
        sc.parse_bulk_text("# 주석만\n\n", NOW)


def test_bulk_title_truncated():
    items, _ = sc.parse_bulk_text("2026-11-20 | " + "가" * 300, NOW)
    assert len(items[0].title) == sc.TITLE_MAX


def test_bulk_add_dedup_and_transaction(store):
    add(store, "기존", datetime(2026, 11, 20, 14))
    items, _ = sc.parse_bulk_text("2026-11-20 14:00 | 기존\n2026-11-21 | 새것\n2026-11-21 | 새것\n2026-11-21 | 다른것", NOW)
    assert store.bulk_add(G1, CH, 10, items) == (2, 2)
    assert store.bulk_add(G1, CH, 10, items) == (0, 4)  # 다시 넣어도 전부 중복
    titles = [e.title for e in store.list_range(G1, date(2026, 11, 1), date(2026, 12, 1))]
    assert sorted(titles) == ["기존", "다른것", "새것"]
    # 다른 서버는 중복 판정과 무관
    assert store.bulk_add(G2, CH, 10, items) == (3, 1)


def test_bulk_add_rollback_on_error(store, monkeypatch):
    items, _ = sc.parse_bulk_text("2026-11-21 | a\n2026-11-22 | b", NOW)
    items[1].title = None  # NOT NULL 위반 → 전체 롤백
    with pytest.raises(sqlite3.IntegrityError):
        store.bulk_add(G1, CH, 10, items)
    assert store.list_range(G1, date(2026, 11, 1), date(2026, 12, 1)) == []


def test_bulk_reminders_scheduled(store):
    items, _ = sc.parse_bulk_text("2026-11-20 14:00 | x | | 30", NOW)
    store.bulk_add(G1, CH, 10, items)
    ev = store.list_range(G1, date(2026, 11, 20), date(2026, 11, 21))[0]
    assert store.remind_at(ev.id) == datetime(2026, 11, 20, 13, 30)


def test_export_import_roundtrip(store):
    add(store, "회의 | 중요", datetime(2026, 11, 20, 14), desc="줄1\n줄2", remind=30)
    add(store, "휴가", datetime(2026, 11, 21), all_day=True, remind=None)
    add(store, "즉시", datetime(2026, 11, 22, 9, 5), remind=0)
    events = store.list_range(G1, *sc.month_range(2026, 11))
    text = sc.export_events_text(events)
    items, errors = sc.parse_bulk_text(text, NOW)
    assert errors == []
    got = [(i.start, i.all_day, i.title, i.description, i.remind_minutes) for i in items]
    assert got == [
        (datetime(2026, 11, 20, 14), False, "회의 ｜ 중요", "줄1 줄2", 30),
        (datetime(2026, 11, 21), True, "휴가", "", None),
        (datetime(2026, 11, 22, 9, 5), False, "즉시", "", 0),
    ]
    # 다른 서버로 왕복 등록
    assert store.bulk_add(G2, CH, 1, items) == (3, 0)


def test_example_file_parses():
    items, errors = sc.parse_bulk_text(open(EXAMPLE, encoding="utf-8").read(), NOW)
    assert errors == [] and len(items) == 5


def test_bulk_preview_embed_limits():
    items, errors = sc.parse_bulk_text("\n".join(["2026-11-20 | " + "가" * 100] * 50 + ["잘못된 줄 " * 20] * 200), NOW)
    emb = sc.build_bulk_preview(items, errors)
    assert "50건" in emb.description and "200줄" in emb.description
    assert all(len(f.value) <= sc.EMBED_FIELD_VALUE_LIMIT for f in emb.fields)
    assert len(emb) <= sc.EMBED_TOTAL_LIMIT


def make_attachment(data: bytes, filename="일정.txt"):
    att = MagicMock()
    att.filename = filename
    att.size = len(data)
    att.read = AsyncMock(return_value=data)
    return att


async def test_cmd_bulk_add_file_preview_then_confirm(store):
    it = make_it()
    att = make_attachment("2026-11-20 14:00 | 팀 회의\n2026-11-25 | 창립기념일\n잘못\n".encode("cp949"))
    await sc.cmd_bulk_add.callback(it, att)
    kw = sent(it).kwargs
    assert kw["ephemeral"] is True and "2건" in kw["embed"].description and "1줄" in kw["embed"].description
    view = kw["view"]
    assert store.list_range(G1, *sc.month_range(2026, 11)) == []  # 확인 전에는 등록 안 됨
    click = make_it()
    click.response.edit_message = AsyncMock()
    await view.do_confirm(click)
    assert "2건" in click.response.edit_message.await_args.kwargs["content"]
    assert len(store.list_range(G1, *sc.month_range(2026, 11))) == 2
    await view.do_confirm(click)  # 두 번 눌러도 중복 등록 없음
    assert len(store.list_range(G1, *sc.month_range(2026, 11))) == 2


async def test_bulk_cancel(store):
    view = sc.BulkConfirmView(10, G1, CH, sc.parse_bulk_text("2026-11-20 | x", NOW)[0])
    click = make_it()
    click.response.edit_message = AsyncMock()
    await view.do_cancel(click)
    assert store.list_range(G1, *sc.month_range(2026, 11)) == []


async def test_bulk_view_only_owner(store):
    view = sc.BulkConfirmView(10, G1, CH, [])
    assert await view.interaction_check(make_it(user_id=10)) is True
    other = make_it(user_id=99)
    assert await view.interaction_check(other) is False
    assert sent(other).kwargs["ephemeral"] is True


async def test_cmd_bulk_add_without_file_opens_modal(store):
    it = make_it()
    it.response.send_modal = AsyncMock()
    await sc.cmd_bulk_add.callback(it, None)
    assert isinstance(it.response.send_modal.await_args.args[0], sc.BulkModal)


async def test_bulk_modal_submit(store):
    modal = sc.BulkModal()
    modal.body._value = "2026-11-20 14:00 | 모달 일정"
    it = make_it()
    await modal.on_submit(it)
    assert "1건" in sent(it).kwargs["embed"].description


@pytest.mark.parametrize("name,data,msg", [
    ("a.png", b"x", ".txt"),
    ("a.txt", b"\xff\xfe\x80\x81\xff", "인코딩"),
    ("a.txt", b"# only\n", "없습니다"),
])
async def test_cmd_bulk_add_file_errors(store, name, data, msg):
    it = make_it()
    await sc.cmd_bulk_add.callback(it, make_attachment(data, name))
    assert sent(it).kwargs["ephemeral"] is True and msg in sent(it).args[0]


async def test_cmd_bulk_add_too_big(store):
    it = make_it()
    att = make_attachment(b"x")
    att.size = sc.BULK_MAX_BYTES + 1
    await sc.cmd_bulk_add.callback(it, att)
    assert "너무 큽니다" in sent(it).args[0]
    att.read.assert_not_awaited()


async def test_cmd_export(store):
    add(store, "이번달", datetime(2026, 11, 20, 14))
    add(store, "남의 서버", datetime(2026, 11, 20, 14), guild=G2)
    it = make_it()
    await sc.cmd_export.callback(it, "이번달")
    kw = sent(it).kwargs
    f = kw["file"]
    body = f.fp.read().decode("utf-8")
    assert "2026-11-20 14:00 | 이번달 |  | 10" in body and "남의 서버" not in body
    assert kw["ephemeral"] is True and f.filename == "일정.txt"


async def test_cmd_export_all(store):
    add(store, "먼 미래", datetime(2030, 1, 1, 9))
    it = make_it()
    await sc.cmd_export.callback(it, "전체")
    assert "먼 미래" in sent(it).kwargs["file"].fp.read().decode("utf-8")


@pytest.mark.parametrize("line,expected,all_day", [
    ("11월 20일 14:00 | 회의", datetime(2026, 11, 20, 14), False),
    ("2026년 12월 3일 | 행사", datetime(2026, 12, 3), True),
    ("11월 20일 오후 2시 | 회의", datetime(2026, 11, 20, 14), False),
    ("11월 20일 오전 9시 30분 | 회의", datetime(2026, 11, 20, 9, 30), False),
    ("11월 20일 오후 2시 반 | 회의", datetime(2026, 11, 20, 14, 30), False),
    ("11월 20일 오후 12시 | 점심", datetime(2026, 11, 20, 12), False),
    ("11월 20일 오전 12시 | 자정", datetime(2026, 11, 20, 0), False),
    ("11월 20일 오후 2:15 | 회의", datetime(2026, 11, 20, 14, 15), False),
    ("11월20일 15시 | 붙여쓰기", datetime(2026, 11, 20, 15), False),
    ("11월 20일 3시 | 오전오후 없으면 24시간제", datetime(2026, 11, 20, 3), False),
    ("2027년1월3일 | 붙여쓰기", datetime(2027, 1, 3), True),
    ("1월 3일 | 연도 생략", datetime(2027, 1, 3), True),
    ("11월 5일 오후 2시 | 이미 지남", datetime(2027, 11, 5, 14), False),
    ("2026-11-20 오후 2시 | 숫자날짜+한국어시각", datetime(2026, 11, 20, 14), False),
])
def test_bulk_korean_dates(line, expected, all_day):
    items, errors = sc.parse_bulk_text(line, NOW)
    assert errors == [], errors
    assert items[0].start == expected and items[0].all_day is all_day


def test_bulk_korean_dates_csv():
    text = "날짜,시각,제목\n11월 20일,오후 2시,회의\n2026년 12월 3일,,행사\n11월 31일,,없는날\n"
    items, errors = sc.parse_bulk_text(text, NOW)
    assert [(i.start, i.all_day) for i in items] == [(datetime(2026, 11, 20, 14), False), (datetime(2026, 12, 3), True)]
    assert errors == [(4, "존재하지 않는 날짜 '11월 31일'")]
