"""attendance.py 테스트 (디스코드 연결 없이 mock + 임시 SQLite 로 검증)."""
import os
import sqlite3
import sys
import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import attendance as att  # noqa: E402

UTC = pytz.utc
SEOUL = pytz.timezone("Asia/Seoul")


def kst(y, mo, d, h=12, mi=0, s=0):
    return SEOUL.localize(datetime(y, mo, d, h, mi, s))


@pytest.fixture
def store(tmp_path):
    return att.AttendanceStore(str(tmp_path / "att.db"))


def make_interaction(guild_id=1, user_id=100, name="철수"):
    it = MagicMock()
    it.guild_id = guild_id
    it.user = SimpleNamespace(id=user_id, display_name=name, mention=f"<@{user_id}>")
    it.response.send_message = AsyncMock()
    it.response.is_done = MagicMock(return_value=False)
    it.followup.send = AsyncMock()
    it.guild.get_member = MagicMock(return_value=None)
    return it


def sent(it):
    args, kwargs = it.response.send_message.call_args
    return (args[0] if args else kwargs.get("content")), kwargs


# ───────────── 날짜 경계 ─────────────

class TestTodayKst:
    def test_utc_before_kst_midnight(self):
        assert att.today_kst(UTC.localize(datetime(2026, 11, 4, 14, 59, 59))) == date(2026, 11, 4)

    def test_utc_at_kst_midnight(self):
        assert att.today_kst(UTC.localize(datetime(2026, 11, 4, 15, 0, 0))) == date(2026, 11, 5)

    def test_naive_is_treated_as_utc(self):
        assert att.today_kst(datetime(2026, 11, 4, 15, 0)) == date(2026, 11, 5)
        assert att.today_kst(datetime(2026, 11, 4, 14, 59)) == date(2026, 11, 4)

    def test_aware_kst(self):
        assert att.today_kst(kst(2026, 1, 1, 0, 0)) == date(2026, 1, 1)
        assert att.today_kst(kst(2025, 12, 31, 23, 59, 59)) == date(2025, 12, 31)

    def test_other_timezone(self):
        ny = pytz.timezone("America/New_York").localize(datetime(2026, 3, 1, 11, 0))  # = KST 3/2 01:00
        assert att.today_kst(ny) == date(2026, 3, 2)

    def test_default_now(self):
        before = datetime.now(SEOUL).date()
        got = att.today_kst()
        after = datetime.now(SEOUL).date()
        assert got in (before, after)  # 자정 순간 호출돼도 둘 중 하나


# ───────────── 포인트 계산 ─────────────

class TestPoints:
    def test_day1_is_base_only(self):
        assert att.calc_points(1) == {"base": 10, "streak_bonus": 0, "weekly_bonus": 0, "total": 10}

    @pytest.mark.parametrize("streak,bonus", [(2, 2), (3, 4), (6, 10), (10, 18), (11, 20), (12, 20), (100, 20)])
    def test_streak_bonus_capped(self, streak, bonus):
        assert att.calc_points(streak)["streak_bonus"] == bonus

    @pytest.mark.parametrize("streak", [7, 14, 21, 70, 700])
    def test_weekly_bonus_every_7(self, streak):
        p = att.calc_points(streak)
        assert p["weekly_bonus"] == att.WEEKLY_BONUS
        assert p["total"] == p["base"] + p["streak_bonus"] + att.WEEKLY_BONUS

    @pytest.mark.parametrize("streak", [1, 6, 8, 13, 15, 20, 22])
    def test_no_weekly_bonus_otherwise(self, streak):
        assert att.calc_points(streak)["weekly_bonus"] == 0

    def test_day7_total(self):
        assert att.calc_points(7)["total"] == 10 + 12 + 50

    @pytest.mark.parametrize("bad", [0, -1])
    def test_invalid_streak(self, bad):
        with pytest.raises(ValueError):
            att.calc_points(bad)


class TestStreakHelpers:
    def test_first_ever(self):
        assert att.next_streak(None, date(2026, 11, 5), 0) == 1

    def test_continues_from_yesterday(self):
        assert att.next_streak("2026-11-04", date(2026, 11, 5), 3) == 4

    def test_breaks_after_gap(self):
        assert att.next_streak("2026-11-03", date(2026, 11, 5), 9) == 1

    def test_month_and_year_boundary(self):
        assert att.next_streak("2026-10-31", date(2026, 11, 1), 2) == 3
        assert att.next_streak("2025-12-31", date(2026, 1, 1), 5) == 6
        assert att.next_streak("2028-02-28", date(2028, 2, 29), 1) == 2  # 윤년

    def test_effective_streak(self):
        t = date(2026, 11, 5)
        assert att.effective_streak("2026-11-05", 4, t) == 4
        assert att.effective_streak("2026-11-04", 4, t) == 4   # 오늘 아직 안 했어도 유지
        assert att.effective_streak("2026-11-03", 4, t) == 0   # 끊김
        assert att.effective_streak(None, 0, t) == 0


# ───────────── 저장소 ─────────────

class TestCheckIn:
    def test_first_checkin(self, store):
        r = store.check_in(1, 100, kst(2026, 11, 5))
        assert not r.already
        assert (r.streak, r.total, r.best_streak, r.points, r.awarded) == (1, 1, 1, 10, 10)
        assert r.date == "2026-11-05"

    def test_twice_same_day(self, store):
        store.check_in(1, 100, kst(2026, 11, 5, 0, 0))
        r = store.check_in(1, 100, kst(2026, 11, 5, 23, 59, 59))
        assert r.already and r.awarded == 0
        assert (r.total, r.points, r.streak) == (1, 10, 1)

    def test_midnight_boundary_counts_as_new_day(self, store):
        store.check_in(1, 100, UTC.localize(datetime(2026, 11, 4, 14, 59, 59)))  # KST 11/4 23:59:59
        r = store.check_in(1, 100, UTC.localize(datetime(2026, 11, 4, 15, 0, 0)))  # KST 11/5 00:00
        assert not r.already
        assert r.streak == 2 and r.total == 2

    def test_utc_same_day_but_kst_different(self, store):
        # 둘 다 UTC 11/4 이지만 KST 로는 11/4, 11/5
        store.check_in(1, 100, UTC.localize(datetime(2026, 11, 4, 1, 0)))
        r = store.check_in(1, 100, UTC.localize(datetime(2026, 11, 4, 16, 0)))
        assert not r.already and r.streak == 2

    def test_utc_different_day_but_kst_same(self, store):
        # UTC 11/3 15:30 과 UTC 11/4 14:00 → 둘 다 KST 11/4
        store.check_in(1, 100, UTC.localize(datetime(2026, 11, 3, 15, 30)))
        r = store.check_in(1, 100, UTC.localize(datetime(2026, 11, 4, 14, 0)))
        assert r.already

    def test_consecutive_days_and_points(self, store):
        start = kst(2026, 11, 1)
        expected_points = 0
        for i in range(10):
            r = store.check_in(1, 100, start + timedelta(days=i))
            expected_points += att.calc_points(i + 1)["total"]
            assert r.streak == i + 1
            assert r.awarded == att.calc_points(i + 1)["total"]
        assert r.points == expected_points
        assert r.total == 10 and r.best_streak == 10

    def test_weekly_bonus_on_day7_and_14(self, store):
        start = kst(2026, 11, 1)
        awarded = [store.check_in(1, 100, start + timedelta(days=i)).breakdown["weekly_bonus"] for i in range(14)]
        assert [i + 1 for i, w in enumerate(awarded) if w] == [7, 14]

    def test_streak_breaks_after_missed_day(self, store):
        store.check_in(1, 100, kst(2026, 11, 1))
        store.check_in(1, 100, kst(2026, 11, 2))
        store.check_in(1, 100, kst(2026, 11, 3))
        r = store.check_in(1, 100, kst(2026, 11, 5))  # 11/4 결석
        assert r.streak == 1 and r.awarded == 10
        assert r.total == 4 and r.best_streak == 3

    def test_best_streak_kept_after_break(self, store):
        for d in (1, 2, 3, 4, 6, 7):
            r = store.check_in(1, 100, kst(2026, 11, d))
        assert r.streak == 2 and r.best_streak == 4

    def test_best_streak_updates_when_exceeded(self, store):
        for d in (1, 2, 4, 5, 6):
            r = store.check_in(1, 100, kst(2026, 11, d))
        assert r.best_streak == 3

    def test_per_guild_isolation(self, store):
        store.check_in(1, 100, kst(2026, 11, 5))
        r = store.check_in(2, 100, kst(2026, 11, 5))
        assert not r.already and r.total == 1

    def test_per_user_isolation(self, store):
        store.check_in(1, 100, kst(2026, 11, 5))
        r = store.check_in(1, 200, kst(2026, 11, 5))
        assert not r.already and r.total == 1

    def test_unique_constraint_in_schema(self, store):
        store.check_in(1, 100, kst(2026, 11, 5))
        conn = sqlite3.connect(store.path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO checkins VALUES (1, 100, '2026-11-05', 10, 1)")
        conn.close()

    def test_already_returns_current_stats(self, store):
        store.check_in(1, 100, kst(2026, 11, 4))
        store.check_in(1, 100, kst(2026, 11, 5))
        r = store.check_in(1, 100, kst(2026, 11, 5, 18))
        assert r.already and r.streak == 2 and r.total == 2 and r.points == 22

    def test_concurrent_requests_award_once(self, tmp_path):
        path = str(tmp_path / "c.db")
        att.AttendanceStore(path).get_stats(1, 1)  # 스키마 생성
        results, errors = [], []
        barrier = threading.Barrier(8)

        def worker():
            try:
                s = att.AttendanceStore(path)
                barrier.wait()
                results.append(s.check_in(1, 100, kst(2026, 11, 5)))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert sum(not r.already for r in results) == 1
        stats = att.AttendanceStore(path).get_stats(1, 100)
        assert stats["total"] == 1 and stats["points"] == 10

    def test_rollback_on_unexpected_error(self, store, monkeypatch):
        store.check_in(1, 100, kst(2026, 11, 4))
        monkeypatch.setattr(att, "calc_points", MagicMock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            store.check_in(1, 100, kst(2026, 11, 5))
        monkeypatch.undo()
        # 실패한 요청은 기록되지 않아 다시 출석 가능, DB 잠금도 남지 않아야 함
        r = store.check_in(1, 100, kst(2026, 11, 5))
        assert not r.already and r.streak == 2

    def test_lazy_db_creation(self, tmp_path):
        path = tmp_path / "lazy.db"
        att.AttendanceStore(str(path))
        assert not path.exists()

    def test_env_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv(att.DB_ENV, str(tmp_path / "env.db"))
        assert att.AttendanceStore().path == str(tmp_path / "env.db")

    def test_default_path(self, monkeypatch):
        monkeypatch.delenv(att.DB_ENV, raising=False)
        assert att.AttendanceStore().path == "attendance.db"

    def test_get_stats_none(self, store):
        assert store.get_stats(1, 999) is None


class TestMonthDays:
    def test_only_this_month(self, store):
        for d in (date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 3), date(2026, 12, 1)):
            store.check_in(1, 100, kst(d.year, d.month, d.day))
        assert store.month_days(1, 100, 2026, 11) == {1, 3}
        assert store.month_days(1, 100, 2026, 10) == {31}

    def test_other_user_excluded(self, store):
        store.check_in(1, 100, kst(2026, 11, 1))
        store.check_in(1, 200, kst(2026, 11, 2))
        assert store.month_days(1, 100, 2026, 11) == {1}

    def test_empty(self, store):
        assert store.month_days(1, 100, 2026, 11) == set()


class TestRanking:
    def _seed(self, store):
        # user 1: 11/1~11/5 (5연속), user 2: 11/1~11/3 (끊김, 누적 3), user 3: 11/5 만
        for d in range(1, 6):
            store.check_in(1, 1, kst(2026, 11, d))
        for d in range(1, 4):
            store.check_in(1, 2, kst(2026, 11, d))
        store.check_in(1, 3, kst(2026, 11, 5))
        store.check_in(9, 4, kst(2026, 11, 5))  # 다른 서버

    def test_points_order(self, store):
        self._seed(store)
        rows = store.ranking(1, "points", now=kst(2026, 11, 5))
        assert [r["user_id"] for r in rows] == [1, 2, 3]
        pts = [r["points"] for r in rows]
        assert pts == sorted(pts, reverse=True)

    def test_streak_ignores_broken_streaks(self, store):
        self._seed(store)
        rows = store.ranking(1, "streak", now=kst(2026, 11, 5))
        assert [(r["user_id"], r["streak"]) for r in rows] == [(1, 5), (3, 1)]

    def test_streak_kept_until_end_of_next_day(self, store):
        self._seed(store)
        rows = store.ranking(1, "streak", now=kst(2026, 11, 6, 23, 59))
        assert [(r["user_id"], r["streak"]) for r in rows] == [(1, 5), (3, 1)]
        assert store.ranking(1, "streak", now=kst(2026, 11, 7)) == []

    def test_total_order(self, store):
        self._seed(store)
        rows = store.ranking(1, "total", now=kst(2026, 11, 5))
        assert [(r["user_id"], r["total"]) for r in rows] == [(1, 5), (2, 3), (3, 1)]

    def test_tie_break_by_user_id(self, store):
        for uid in (30, 10, 20):
            store.check_in(1, uid, kst(2026, 11, 5))
        rows = store.ranking(1, "points", now=kst(2026, 11, 5))
        assert [r["user_id"] for r in rows] == [10, 20, 30]

    def test_limit_10(self, store):
        for uid in range(1, 16):
            for d in range(1, uid % 5 + 2):
                store.check_in(1, uid, kst(2026, 11, d))
        rows = store.ranking(1, "points", now=kst(2026, 11, 5))
        assert len(rows) == 10
        assert [r["points"] for r in rows] == sorted((r["points"] for r in rows), reverse=True)

    def test_guild_isolation(self, store):
        self._seed(store)
        assert [r["user_id"] for r in store.ranking(9, "points", now=kst(2026, 11, 5))] == [4]

    def test_invalid_key(self, store):
        with pytest.raises(ValueError):
            store.ranking(1, "user_id; DROP TABLE stats")

    def test_empty(self, store):
        assert store.ranking(1, "points") == []


class TestCalendar:
    def test_marks_days(self):
        out = att.render_calendar(2026, 11, {4, 5})
        assert out.startswith("```") and out.endswith("```")
        assert "2026년 11월" in out
        assert out.count("✅") == 2
        assert " 4" not in out.split("\n")[4]  # 11/4 행에서 숫자 대신 ✅

    def test_layout_monday_first(self):
        lines = att.render_calendar(2026, 11, set()).strip("`\n").split("\n")
        assert lines[1] == "월 화 수 목 금 토 일"
        assert lines[2].endswith(" 1")  # 2026-11-01 은 일요일
        assert lines[-1].strip() == "30"

    def test_all_days(self):
        out = att.render_calendar(2026, 2, set(range(1, 29)))
        assert out.count("✅") == 28

    def test_leap_february(self):
        assert "29" in att.render_calendar(2028, 2, set())
        assert "29" not in att.render_calendar(2026, 2, set())


# ───────────── 명령 핸들러 ─────────────

class TestHandlers:
    async def test_checkin_success_public(self, store):
        it = make_interaction()
        await att.handle_checkin(it, store, kst(2026, 11, 5))
        text, kw = sent(it)
        assert "출석 완료" in text and "+10P" in text
        assert not kw.get("ephemeral")
        am = kw["allowed_mentions"]
        assert am.everyone is False and am.roles is False and am.users is False

    async def test_checkin_already_ephemeral(self, store):
        it = make_interaction()
        await att.handle_checkin(it, store, kst(2026, 11, 5))
        it2 = make_interaction()
        await att.handle_checkin(it2, store, kst(2026, 11, 5, 20))
        text, kw = sent(it2)
        assert "이미 출석" in text and kw.get("ephemeral") is True

    async def test_checkin_weekly_bonus_message(self, store):
        for d in range(1, 7):
            store.check_in(1, 100, kst(2026, 11, d))
        it = make_interaction()
        await att.handle_checkin(it, store, kst(2026, 11, 7))
        text, _ = sent(it)
        assert "7일 보너스 50" in text and "연속 7일" in text

    async def test_checkin_escapes_name_and_no_ping(self, store):
        it = make_interaction(name="@everyone **굵게**")
        await att.handle_checkin(it, store, kst(2026, 11, 5))
        text, kw = sent(it)
        assert "\\*\\*" in text
        assert kw["allowed_mentions"].everyone is False

    async def test_checkin_dm_rejected(self, store):
        it = make_interaction(guild_id=None)
        assert await att.handle_checkin(it, store) is None
        _, kw = sent(it)
        assert kw.get("ephemeral") is True

    async def test_status_self(self, store):
        store.check_in(1, 100, kst(2026, 11, 4))
        store.check_in(1, 100, kst(2026, 11, 5))
        it = make_interaction()
        await att.handle_status(it, store, now=kst(2026, 11, 5, 22))
        _, kw = sent(it)
        e = kw["embed"]
        fields = {f.name: f.value for f in e.fields}
        assert fields["누적 출석"] == "2일" and fields["연속 출석"] == "2일"
        assert fields["최장 연속"] == "2일" and fields["포인트"] == "22P"
        assert fields["오늘 출석"].startswith("완료")
        cal = [v for k, v in fields.items() if k.startswith("이번 달")][0]
        assert cal.count("✅") == 2

    async def test_status_other_user_and_broken_streak(self, store):
        store.check_in(1, 200, kst(2026, 11, 1))
        store.check_in(1, 200, kst(2026, 11, 2))
        it = make_interaction()
        other = SimpleNamespace(id=200, display_name="영희")
        await att.handle_status(it, store, other, now=kst(2026, 11, 5))
        e = sent(it)[1]["embed"]
        fields = {f.name: f.value for f in e.fields}
        assert "영희" in e.title
        assert fields["연속 출석"] == "0일" and fields["최장 연속"] == "2일"
        assert fields["오늘 출석"].startswith("아직")

    async def test_status_no_record(self, store):
        it = make_interaction()
        await att.handle_status(it, store, now=kst(2026, 11, 5))
        fields = {f.name: f.value for f in sent(it)[1]["embed"].fields}
        assert fields["누적 출석"] == "0일" and fields["포인트"] == "0P"

    async def test_status_month_boundary(self, store):
        store.check_in(1, 100, kst(2026, 10, 31))
        it = make_interaction()
        await att.handle_status(it, store, now=kst(2026, 11, 1))
        fields = {f.name: f.value for f in sent(it)[1]["embed"].fields}
        assert fields["이번 달 (0일)"].count("✅") == 0
        assert fields["연속 출석"] == "1일"

    async def test_status_dm_rejected(self, store):
        it = make_interaction(guild_id=None)
        await att.handle_status(it, store)
        assert sent(it)[1].get("ephemeral") is True

    async def test_ranking_embed(self, store):
        store.check_in(1, 1, kst(2026, 11, 4))
        store.check_in(1, 1, kst(2026, 11, 5))
        store.check_in(1, 2, kst(2026, 11, 5))
        it = make_interaction()
        it.guild.get_member = MagicMock(side_effect=lambda uid: SimpleNamespace(display_name=f"u{uid}") if uid == 1 else None)
        await att.handle_ranking(it, store, "포인트", now=kst(2026, 11, 5))
        e = sent(it)[1]["embed"]
        lines = e.description.split("\n")
        assert lines[0].startswith("🥇") and "u1" in lines[0] and "22P" in lines[0]
        assert lines[1].startswith("🥈") and "<@2>" in lines[1]

    async def test_ranking_streak_label(self, store):
        store.check_in(1, 1, kst(2026, 11, 5))
        it = make_interaction()
        await att.handle_ranking(it, store, "연속", now=kst(2026, 11, 5))
        assert "1일 연속" in sent(it)[1]["embed"].description

    async def test_ranking_empty(self, store):
        it = make_interaction()
        await att.handle_ranking(it, store, "누적")
        assert "기록이 없습니다" in sent(it)[1]["embed"].description

    async def test_ranking_bad_key_ephemeral(self, store):
        it = make_interaction()
        await att.handle_ranking(it, store, "몰라")
        assert sent(it)[1].get("ephemeral") is True

    async def test_ranking_more_than_ten_shows_ten(self, store):
        for uid in range(1, 13):
            store.check_in(1, uid, kst(2026, 11, 5))
        it = make_interaction()
        await att.handle_ranking(it, store, "포인트", now=kst(2026, 11, 5))
        lines = sent(it)[1]["embed"].description.split("\n")
        assert len(lines) == 10 and "`10.`" in lines[-1]

    async def test_error_uses_followup_when_responded(self, store):
        it = make_interaction(guild_id=None)
        it.response.is_done = MagicMock(return_value=True)
        await att.handle_checkin(it, store)
        it.followup.send.assert_awaited_once()
        assert it.followup.send.call_args.kwargs["ephemeral"] is True


class TestRegister:
    def test_register_adds_commands(self, tmp_path):
        from discord import app_commands
        fake = SimpleNamespace(tree=MagicMock())
        store = att.register(fake, att.AttendanceStore(str(tmp_path / "r.db")))
        names = [c.args[0].name for c in fake.tree.add_command.call_args_list]
        assert names == ["출석", "출석현황", "랭킹"]
        assert all(isinstance(c.args[0], app_commands.Command) for c in fake.tree.add_command.call_args_list)
        assert fake.attendance_store is store

    def test_ranking_choices(self, tmp_path):
        fake = SimpleNamespace(tree=MagicMock())
        att.register(fake, att.AttendanceStore(str(tmp_path / "r.db")))
        cmd = fake.tree.add_command.call_args_list[2].args[0]
        assert [c.name for c in cmd.parameters[0].choices] == ["포인트", "연속", "누적"]

    def test_bot_registers_commands(self):
        import bot
        names = {c.name for c in bot.bot.tree.get_commands()}
        assert {"출석", "출석현황", "랭킹", "예약"} <= names

    async def test_command_callback_invokes_handler(self, tmp_path):
        fake = SimpleNamespace(tree=MagicMock())
        store = att.AttendanceStore(str(tmp_path / "cb.db"))
        att.register(fake, store)
        cmd = fake.tree.add_command.call_args_list[0].args[0]
        it = make_interaction()
        await cmd.callback(it)
        assert "출석 완료" in sent(it)[0]
        assert store.get_stats(1, 100)["total"] == 1
