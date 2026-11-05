"""schedule_calendar.py 테스트 (디스코드 연결 없이 mock 으로 검증)."""
import asyncio
import os
import sys
from datetime import date, datetime, time, timedelta
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
    await sc.cmd_calendar.callback(it)
    emb = sent(it).kwargs["embed"]
    assert emb.title == "📆 2026년 11월"
    assert "```" in emb.description and "[ 5]" in emb.description and "20 *" in emb.description
    assert "21 *" not in emb.description
    assert "행사" in emb.fields[0].value and "남의" not in emb.fields[0].value
    assert "file" not in sent(it).kwargs


async def test_cmd_calendar_specific_month(store):
    it = make_it()
    await sc.cmd_calendar.callback(it, 2028, 2)
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
