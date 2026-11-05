"""time_parser 반복 예약(parse_recurrence / is_recurring) 단위 테스트.

기준 시각(BASE): 2026-11-05(목) 14:00:00 한국 시간.
"""

import dataclasses
import os
import sys
from datetime import datetime

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

import time_parser  # noqa: E402
from time_parser import (  # noqa: E402
    MIN_INTERVAL_SECONDS, Recurrence, TimeParseError, is_recurring, parse_recurrence, parse_time,
)

SEOUL = pytz.timezone("Asia/Seoul")
BASE = SEOUL.localize(datetime(2026, 11, 5, 14, 0, 0))  # 목요일
WEEKDAYS = "mon,tue,wed,thu,fri"


def hms(h, m=0, s=0):
    return {"hour": h, "minute": m, "second": s}


def dow(days, h, m=0, s=0):
    return {"day_of_week": days, **hms(h, m, s)}


# ─────────────────────────────────────────────────────────────────────────────
# 성공 케이스: (입력, 기대 trigger kwargs)
# ─────────────────────────────────────────────────────────────────────────────

DAILY = [
    ("매일 오전 6시", hms(6)),
    ("매일 오전6시", hms(6)),
    ("매일오전6시", hms(6)),
    ("매일 아침 7시 30분", hms(7, 30)),
    ("매일 아침 7시 반", hms(7, 30)),
    ("매일 7:30", hms(7, 30)),
    ("매일 07:30", hms(7, 30)),
    ("매일 0730", hms(7, 30)),
    ("매일 19:45", hms(19, 45)),
    ("매일 오후 3시", hms(15)),
    ("매일 3시", hms(15)),          # 날짜 지정 규칙과 동일: 1~6시는 오후
    ("매일 9시", hms(9)),
    ("매일 12시", hms(12)),
    ("매일 새벽 5시", hms(5)),
    ("매일 저녁 7시", hms(19)),
    ("매일 밤 11시", hms(23)),
    ("매일 밤 12시", hms(0)),
    ("매일 자정", hms(0)),
    ("매일 정오", hms(12)),
    ("매일 오전 9시 15분 30초", hms(9, 15, 30)),
    ("매일 일곱시", hms(7)),
    ("매일 오후 세시 이십분", hms(15, 20)),
    ("매일마다 8시", hms(8)),
    ("매일 8시마다", hms(8)),
    ("매일 8시에", hms(8)),
    ("매일매일 8시", hms(8)),
    ("날마다 오후 10시", hms(22)),
    ("하루마다 8시", hms(8)),
    ("매일 오전 8시 정각", hms(8)),
    ("every day 7am", hms(7)),
    ("everyday 7am", hms(7)),
    ("every day at 7:30 pm", hms(19, 30)),
    ("Every Day 7AM", hms(7)),
    ("daily 7am", hms(7)),
    ("daily at 18:00", hms(18)),
    ("7am every day", hms(7)),
    ("every day noon", hms(12)),
    ("every day midnight", hms(0)),
]

WEEKDAY_GROUPS = [
    ("평일 8시", dow(WEEKDAYS, 8)),
    ("평일 오전 8시 30분", dow(WEEKDAYS, 8, 30)),
    ("평일마다 8시", dow(WEEKDAYS, 8)),
    ("매주 평일 9시", dow(WEEKDAYS, 9)),
    ("주중 9시", dow(WEEKDAYS, 9)),
    ("주말 오전 10시", dow("sat,sun", 10)),
    ("주말마다 11시", dow("sat,sun", 11)),
    ("매주 주말 오후 2시", dow("sat,sun", 14)),
    ("every weekday 8am", dow(WEEKDAYS, 8)),
    ("weekdays 9:30am", dow(WEEKDAYS, 9, 30)),
    ("every weekend 10am", dow("sat,sun", 10)),
    ("weekends 10am", dow("sat,sun", 10)),
]

WEEKLY = [
    ("매주 월요일 9시", dow("mon", 9)),
    ("매주 월요일 오전 9시", dow("mon", 9)),
    ("매주 월욜 9시", dow("mon", 9)),
    ("매주 월 9시", dow("mon", 9)),
    ("매주 화요일 오후 3시", dow("tue", 15)),
    ("매주 화요일 오후 3시마다", dow("tue", 15)),
    ("매주 수요일 저녁 7시", dow("wed", 19)),
    ("매주 목요일 10:30", dow("thu", 10, 30)),
    ("매주 금요일 오후 6시", dow("fri", 18)),
    ("매주 토요일 오전 11시", dow("sat", 11)),
    ("매주 일요일 오후 8시", dow("sun", 20)),
    ("매주 일요일 아침 7시", dow("sun", 7)),
    ("매주 월수금 오전 7시", dow("mon,wed,fri", 7)),
    ("매주 월 수 금 7시", dow("mon,wed,fri", 7)),
    ("매주 월, 수, 금 7시", dow("mon,wed,fri", 7)),
    ("매주 월/수/금 7시", dow("mon,wed,fri", 7)),
    ("매주 월·수·금 7시", dow("mon,wed,fri", 7)),
    ("매주 월요일 수요일 금요일 7시", dow("mon,wed,fri", 7)),
    ("매주 월요일과 목요일 9시", dow("mon,thu", 9)),
    ("매주 화목 오후 2시", dow("tue,thu", 14)),
    ("매주 금월 9시", dow("mon,fri", 9)),  # 순서와 무관
    ("매주 월월 9시", dow("mon", 9)),      # 중복 제거
    ("매주 토일 10시", dow("sat,sun", 10)),
    ("매주 월~금 9시", dow(WEEKDAYS, 9)),
    ("매주 월 ~ 금 9시", dow(WEEKDAYS, 9)),
    ("매주 월-금 9시", dow(WEEKDAYS, 9)),
    ("매주 월요일~금요일 9시", dow(WEEKDAYS, 9)),
    ("매주 월요일부터 금요일까지 9시", dow(WEEKDAYS, 9)),
    ("매주 금~월 9시", dow("mon,fri,sat,sun", 9)),  # 주를 넘어가는 범위
    ("매주 화~목 9시", dow("tue,wed,thu", 9)),
    ("매주 월~일 9시", hms(9)),          # 7일 전부 = 매일
    ("월요일마다 오후 6시", dow("mon", 18)),
    ("금요일마다 6시", dow("fri", 18)),
    ("월수금마다 9시", dow("mon,wed,fri", 9)),
    ("주마다 월요일 9시", dow("mon", 9)),
    # 밤 늦은 시각/자정은 다음날로 넘어감
    ("매주 금요일 밤 12시", dow("sat", 0)),
    ("매주 금요일 자정", dow("sat", 0)),
    ("매주 월요일 밤 1시", dow("tue", 1)),
    ("매주 일요일 밤 1시", dow("mon", 1)),
    ("평일 밤 12시", dow("tue,wed,thu,fri,sat", 0)),
    # 영어
    ("every monday 9am", dow("mon", 9)),
    ("every mon 9am", dow("mon", 9)),
    ("every friday at 6pm", dow("fri", 18)),
    ("every sunday 8:30pm", dow("sun", 20, 30)),
    ("every mon, wed, fri 9am", dow("mon,wed,fri", 9)),
    ("every mon wed fri 9am", dow("mon,wed,fri", 9)),
    ("every monday and thursday 6pm", dow("mon,thu", 18)),
    ("every mon-fri 8am", dow(WEEKDAYS, 8)),
    ("every monday to friday 8am", dow(WEEKDAYS, 8)),
    ("every tue/thu 7pm", dow("tue,thu", 19)),
    ("weekly on monday 9am", dow("mon", 9)),
    ("every saturday noon", dow("sat", 12)),
]

MONTHLY = [
    ("매월 1일 오전 9시", {"day": 1, **hms(9)}),
    ("매월 1일 9시", {"day": 1, **hms(9)}),
    ("매달 15일 오후 3시", {"day": 15, **hms(15)}),
    ("매월 15일마다 오후 3시", {"day": 15, **hms(15)}),
    ("달마다 10일 10시", {"day": 10, **hms(10)}),
    ("매월 25일 18:00", {"day": 25, **hms(18)}),
    ("매월 이십오일 열시", {"day": 25, **hms(10)}),
    ("매월 31일 9시", {"day": 31, **hms(9)}),
    ("매월 말일 오후 6시", {"day": "last", **hms(18)}),
    ("매달 마지막날 오후 6시", {"day": "last", **hms(18)}),
    ("every month on the 15th 9am", {"day": 15, **hms(9)}),
    ("monthly on the 1st 10am", {"day": 1, **hms(10)}),
    ("every month on the 22nd 7pm", {"day": 22, **hms(19)}),
    ("every month on the last day 6pm", {"day": "last", **hms(18)}),
    ("매년 12월 25일 오전 9시", {"month": 12, "day": 25, **hms(9)}),
    ("매년 1월 1일 0시", {"month": 1, "day": 1, **hms(0)}),
    ("해마다 5월 5일 10시", {"month": 5, "day": 5, **hms(10)}),
    ("매년 2월 29일 9시", {"month": 2, "day": 29, **hms(9)}),
]

HOURLY = [
    ("매시간", {"minute": 0, "second": 0}),
    ("매시간마다", {"minute": 0, "second": 0}),
    ("매시간 정각", {"minute": 0, "second": 0}),
    ("매시 정각", {"minute": 0, "second": 0}),
    ("매시 30분", {"minute": 30, "second": 0}),
    ("매시간 15분", {"minute": 15, "second": 0}),
    ("every hour", {"minute": 0, "second": 0}),
    ("hourly", {"minute": 0, "second": 0}),
]

INTERVAL = [
    ("10분마다", 600),
    ("30분마다", 1800),
    ("30 분 마다", 1800),
    ("삼십분마다", 1800),
    ("반시간마다", 1800),
    ("45분마다", 2700),
    ("1시간마다", 3600),
    ("한시간마다", 3600),
    ("2시간마다", 7200),
    ("두시간마다", 7200),
    ("세 시간마다", 10800),
    ("1시간 30분마다", 5400),
    ("1시간반마다", 5400),
    ("1.5시간마다", 5400),
    ("2시간 간격으로", 7200),
    ("3시간에 한번씩", 10800),
    ("168시간마다", 168 * 3600),
    ("every 10 minutes", 600),
    ("every 30 minutes", 1800),
    ("every 30 mins", 1800),
    ("every 30m", 1800),
    ("every 2 hours", 7200),
    ("every 2h", 7200),
    ("every 1 hour 30 minutes", 5400),
    ("every 1h30m", 5400),
    ("every half hour", 1800),
]


@pytest.mark.parametrize("text,expected", DAILY + WEEKDAY_GROUPS + WEEKLY + MONTHLY + HOURLY)
def test_cron_rules(text, expected):
    r = parse_recurrence(text, BASE)
    assert r.kind == "cron"
    assert r.trigger_kwargs() == expected
    CronTrigger(timezone="Asia/Seoul", **r.trigger_kwargs())  # APScheduler 가 받아들이는 인자인지


@pytest.mark.parametrize("text,seconds", INTERVAL)
def test_interval_rules(text, seconds):
    r = parse_recurrence(text, BASE)
    assert r.kind == "interval"
    assert r.interval_seconds == seconds
    assert r.trigger_kwargs() == {"seconds": seconds}
    IntervalTrigger(timezone="Asia/Seoul", **r.trigger_kwargs())


ALL_OK = [c[0] for c in DAILY + WEEKDAY_GROUPS + WEEKLY + MONTHLY + HOURLY + INTERVAL]


@pytest.mark.parametrize("text", ALL_OK)
def test_all_recurring_inputs_are_detected(text):
    assert is_recurring(text)


@pytest.mark.parametrize("text", ALL_OK)
def test_parse_time_still_rejects_recurring(text):
    """기존 parse_time 은 반복 표현을 1회성 시각으로 잘못 해석하지 않아야 한다."""
    with pytest.raises(TimeParseError):
        parse_time(text, BASE.replace(tzinfo=None))


@pytest.mark.parametrize("text", ALL_OK)
def test_trigger_kwargs_are_primitive(text):
    """SQLAlchemyJobStore 등 영구 저장소와 호환되도록 기본 타입만 사용."""
    kw = parse_recurrence(text, BASE).trigger_kwargs()
    assert all(isinstance(k, str) and type(v) in (int, str) for k, v in kw.items())


# ─────────────────────────────────────────────────────────────────────────────
# 설명 문자열
# ─────────────────────────────────────────────────────────────────────────────

DESCRIPTIONS = [
    ("매일 오전 6시", "매일 오전 6시"),
    ("매일 아침 7시 30분", "매일 오전 7시 30분"),
    ("매일 오전 9시 15분 30초", "매일 오전 9시 15분 30초"),
    ("매일 자정", "매일 자정(0시)"),
    ("매일 정오", "매일 정오(낮 12시)"),
    ("매일 오후 12시 30분", "매일 오후 12시 30분"),
    ("매일 오전 0시 30분", "매일 오전 12시 30분"),
    ("평일 8시", "평일(월~금) 오전 8시"),
    ("주말 오전 10시", "주말(토·일) 오전 10시"),
    ("매주 월요일 9시", "매주 월요일 오전 9시"),
    ("매주 금월 오후 6시", "매주 월·금 오후 6시"),
    ("매주 월수금 오전 7시", "매주 월·수·금 오전 7시"),
    ("매주 금요일 밤 12시", "매주 토요일 자정(0시)"),
    ("매월 1일 오전 9시", "매월 1일 오전 9시"),
    ("매월 31일 9시", "매월 31일 오전 9시 (해당 날짜가 없는 달은 건너뜀)"),
    ("매월 말일 오후 6시", "매월 말일 오후 6시"),
    ("매년 12월 25일 오전 9시", "매년 12월 25일 오전 9시"),
    ("매년 2월 29일 9시", "매년 2월 29일 오전 9시 (윤년에만)"),
    ("매시간", "매시간 정각"),
    ("매시 30분", "매시 30분"),
    ("30분마다", "30분마다"),
    ("2시간마다", "2시간마다"),
    ("1시간 30분마다", "1시간 30분마다"),
    ("every 90 minutes", "1시간 30분마다"),
]


@pytest.mark.parametrize("text,desc", DESCRIPTIONS)
def test_description(text, desc):
    assert parse_recurrence(text, BASE).description == desc


# ─────────────────────────────────────────────────────────────────────────────
# 다음 실행 시각 (APScheduler 트리거로 실제 계산)
# ─────────────────────────────────────────────────────────────────────────────

def next_fire(text):
    r = parse_recurrence(text, BASE)
    if r.kind == "interval":
        trig = IntervalTrigger(timezone="Asia/Seoul", start_date=BASE, **r.trigger_kwargs())
    else:
        trig = CronTrigger(timezone="Asia/Seoul", **r.trigger_kwargs())
    nxt = trig.get_next_fire_time(None, BASE)
    return nxt.replace(tzinfo=None) if nxt else None


NEXT_FIRES = [
    ("매일 오전 6시", datetime(2026, 11, 6, 6, 0)),     # 오늘 6시는 지남
    ("매일 오후 3시", datetime(2026, 11, 5, 15, 0)),    # 오늘 15시
    ("매일 오후 2시", datetime(2026, 11, 5, 14, 0)),    # 기준 시각과 같으면 그 시각 (APScheduler 규칙)
    ("평일 8시", datetime(2026, 11, 6, 8, 0)),           # 금
    ("주말 오전 10시", datetime(2026, 11, 7, 10, 0)),     # 토
    ("매주 월요일 9시", datetime(2026, 11, 9, 9, 0)),
    ("매주 목요일 오후 6시", datetime(2026, 11, 5, 18, 0)),  # 오늘(목)
    ("매주 목요일 오전 9시", datetime(2026, 11, 12, 9, 0)),  # 오늘 지남 → 다음 주
    ("매주 월수금 오전 7시", datetime(2026, 11, 6, 7, 0)),
    ("매주 금요일 밤 12시", datetime(2026, 11, 7, 0, 0)),   # 금요일 밤 12시 = 토 00:00
    ("매월 1일 오전 9시", datetime(2026, 12, 1, 9, 0)),
    ("매월 5일 오후 6시", datetime(2026, 11, 5, 18, 0)),
    ("매월 31일 9시", datetime(2026, 12, 31, 9, 0)),       # 11월엔 31일이 없음
    ("매월 말일 오후 6시", datetime(2026, 11, 30, 18, 0)),
    ("매년 12월 25일 오전 9시", datetime(2026, 12, 25, 9, 0)),
    ("매년 1월 1일 0시", datetime(2027, 1, 1, 0, 0)),
    ("매년 2월 29일 9시", datetime(2028, 2, 29, 9, 0)),
    ("매시간", datetime(2026, 11, 5, 14, 0)),
    ("매시 30분", datetime(2026, 11, 5, 14, 30)),
]


@pytest.mark.parametrize("text,expected", NEXT_FIRES)
def test_next_fire_time(text, expected):
    assert next_fire(text) == expected


def test_interval_next_fire_is_one_interval_later():
    r = parse_recurrence("30분마다", BASE)
    trig = IntervalTrigger(timezone="Asia/Seoul", **r.trigger_kwargs())
    nxt = trig.get_next_fire_time(None, datetime.now(SEOUL))
    delta = (nxt - datetime.now(SEOUL)).total_seconds()
    assert 1790 <= delta <= 1800


def test_cron_runs_in_seoul_time():
    r = parse_recurrence("매일 오전 9시", BASE)
    trig = CronTrigger(timezone="Asia/Seoul", **r.trigger_kwargs())
    nxt = trig.get_next_fire_time(None, BASE)
    assert nxt.utcoffset().total_seconds() == 9 * 3600
    assert nxt.astimezone(pytz.utc).hour == 0  # 09:00 KST = 00:00 UTC


# ─────────────────────────────────────────────────────────────────────────────
# 실패 케이스: (입력, 오류 메시지에 포함되어야 할 문구)
# ─────────────────────────────────────────────────────────────────────────────

FAILURES = [
    ("", "입력"),
    ("   ", "입력"),
    ("매일", "몇 시"),
    ("매주 월요일", "몇 시"),
    ("평일", "몇 시"),
    ("매월 1일", "몇 시"),
    ("매일 오후", "몇 시"),
    ("매주 9시", "요일"),
    ("매주", "요일"),
    ("every week 9am", "요일"),
    ("매월 9시", "날짜"),
    ("매달 오후 3시", "날짜"),
    ("monthly 9am", "날짜"),
    ("매년 9시", "월/일"),
    ("5분마다", "최소 10분"),
    ("9분마다", "최소 10분"),
    ("30초마다", "최소 10분"),
    ("every 5 minutes", "최소 10분"),
    ("169시간마다", "7일"),
    ("1000시간마다", "7일"),
    ("99999999999999999999시간마다", "너무"),
    ("2일마다 9시", "일/주 단위"),
    ("3일마다", "일/주 단위"),
    ("이틀마다 9시", "일/주 단위"),
    ("2주마다", "일/주 단위"),
    ("일주일마다 9시", "일/주 단위"),
    ("every 2 days 9am", "일/주 단위"),
    ("30분마다 9시", "함께 쓸 수 없습니다"),
    ("2시간마다 오후 3시", "함께 쓸 수 없습니다"),
    ("매시간 9시", "함께 쓸 수 없습니다"),
    ("매시 75분", "0~59"),
    ("매일 3시 30분마다", "여러 개"),
    ("매일 평일 9시", "여러 개"),
    ("매일 매주 월요일 9시", "여러 개"),
    ("매일 9시 10시", "여러 개"),
    ("매일 3시 회의", "이해할 수 없는"),
    ("매일 내일 9시", "이해할 수 없는"),
    ("매일 2026년 12월 25일 9시", "여러 개"),
    ("매주 월요일 다음주 9시", "이해할 수 없는"),
    ("매일 오전 25시", "0~12"),
    ("매일 25시", "0~24"),
    ("매일 9시 60분", "0~59"),
    ("매일 오전 9시 오후", "맞지 않"),
    ("매일 오전 오후 9시", "중복"),
    ("매월 32일 9시", "1~31"),
    ("매월 0일 9시", "1~31"),
    ("매년 2월 30일 9시", "존재하지 않는"),
    ("매년 13월 1일 9시", "존재하지 않는"),
    ("매월 말일 자정", "모호"),
    ("매월 1일 밤 1시", "모호"),
    ("매년 1월 1일 자정", "모호"),
    ("내일 오후 3시", "반복 규칙"),
    ("30분 뒤", "반복 규칙"),
]


@pytest.mark.parametrize("text,msg", FAILURES)
def test_failures(text, msg):
    with pytest.raises(TimeParseError) as ei:
        parse_recurrence(text, BASE)
    assert msg in str(ei.value)


def test_none_input():
    with pytest.raises(TimeParseError):
        parse_recurrence(None)


def test_base_time_is_optional():
    assert parse_recurrence("매일 9시").trigger_kwargs() == hms(9)


# ─────────────────────────────────────────────────────────────────────────────
# is_recurring: 1회성 표현은 반복으로 보지 않음 (기존 /예약 동작 보존)
# ─────────────────────────────────────────────────────────────────────────────

ONE_SHOT = [
    "내일 오후 3시", "30분 뒤", "1시간 반 뒤", "15:30", "다음주 월요일 9시", "이번주 토요일 오후 3시",
    "금요일 저녁 7시", "2026년 11월 5일 9시", "12/25 6pm", "tomorrow 7am", "next monday 9am",
    "in 10 minutes", "이번 주말 3시", "다음 주말 3시", "오늘 밤 11시", "자정", "20일 오후 3시",
    "다음달 3일 오후 3시", "금일 3시", "", "   ",
]


@pytest.mark.parametrize("text", ONE_SHOT)
def test_one_shot_not_recurring(text):
    assert is_recurring(text) is False


def test_is_recurring_none():
    assert is_recurring(None) is False


def test_recurrence_is_hashable_and_frozen():
    r = parse_recurrence("매일 9시")
    hash(r)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.kind = "interval"  # type: ignore[misc]
    assert isinstance(r, Recurrence)


def test_min_interval_constant():
    assert MIN_INTERVAL_SECONDS == 600
    parse_recurrence(f"{MIN_INTERVAL_SECONDS // 60}분마다")


def test_public_api_exports():
    for name in ("parse_recurrence", "is_recurring", "Recurrence", "parse_time", "parse_korean_time"):
        assert name in time_parser.__all__
