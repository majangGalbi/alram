import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import os
import pytz
import uuid
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
import dateparser
import re

load_dotenv()

# [1] 24시간 호스팅 유지를 위한 간단한 웹서버 세팅
app = Flask('')

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True  # 봇 종료 시 Flask 스레드도 함께 종료
    t.start()

# [2] 디스코드 봇 설정
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
        self.job_owners = {}  # job_id -> user_id 소유자 매핑

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()
        print("⏰ 스케줄러가 시작되었습니다.")

bot = MyBot()

# 지정된 시간이 되었을 때 실행될 핑 알림 함수
async def send_ping(channel_id, user_mention, message, job_id):
    try:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(f"{user_mention} {message}")
            print(f"🔔 알림 전송 완료: {user_mention}")
        else:
            print(f"⚠️ 채널 {channel_id}을 찾을 수 없어 알림 전송 실패")
    except Exception as e:
        print(f"❌ 알림 전송 중 오류: {e}")
    finally:
        bot.job_owners.pop(job_id, None)

def format_korean_time(dt: datetime) -> str:
    """시스템 로케일에 상관없이 안전하게 한국어 오전/오후 시간을 포맷합니다."""
    ampm = "오전" if dt.hour < 12 else "오후"
    hour_12 = dt.hour % 12 or 12  # 0시 → 12시
    return dt.strftime(f"%Y년 %m월 %d일 {ampm} {hour_12}시 %M분 %S초")

def parse_korean_time(text: str, base_time: datetime):
    """한국어 시간 표현을 파싱합니다.

    1단계: 년/월/일 포함 형식 직접 파싱
      예) 2026년 06월 02일 오전 09시 38분 1초
          2026년 6월 2일 8시 10분
    2단계: 오늘/내일 + 시간, 또는 시간만 있는 형식 직접 파싱
      예) 오늘 9시 31분, 오후 3시, 내일 오전 10시
    3단계: dateparser fallback (순수 상대 표현)
      예) 20분 뒤, 1시간 후
    """
    base_naive = base_time.replace(tzinfo=None)

    # ── 공통: 오전/오후 → 24시간 변환 헬퍼 ────────────────────────────────
    def apply_ampm(hour, ampm):
        if ampm == "오후" and hour != 12:
            return hour + 12
        if ampm == "오전" and hour == 12:
            return 0
        return hour

    # ── 1단계: 년/월/일 포함 ──────────────────────────────────────────────
    m = re.search(
        r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일'
        r'(?:\s*(오전|오후))?'
        r'\s*(\d{1,2})시'
        r'(?:\s*(\d{1,2})분)?'
        r'(?:\s*(\d{1,2})초)?',
        text
    )
    if m:
        year, month, day, ampm, hour, minute, second = m.groups()
        year, month, day = int(year), int(month), int(day)
        hour = apply_ampm(int(hour), ampm)
        minute = int(minute) if minute else 0
        second = int(second) if second else 0
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            pass

    # ── 2단계: 오늘/내일/시간만 ──────────────────────────────────────────
    m = re.search(
        r'(오늘|내일)?'
        r'\s*(?:(오전|오후)\s*)?'
        r'(\d{1,2})시'
        r'(?:\s*(\d{1,2})분)?'
        r'(?:\s*(\d{1,2})초)?',
        text
    )
    if m:
        day_word, ampm, hour, minute, second = m.groups()
        hour = int(hour)
        minute = int(minute) if minute else 0
        second = int(second) if second else 0

        # 오전/오후가 명시된 경우 변환
        if ampm:
            hour = apply_ampm(hour, ampm)
            base_date = base_naive.date()
            if day_word == "내일":
                base_date = base_date + timedelta(days=1)
            try:
                return datetime(base_date.year, base_date.month, base_date.day, hour, minute, second)
            except ValueError:
                pass

        else:
            # 오전/오후 미지정: 가장 가까운 미래 시간을 찾아줌
            base_date = base_naive.date()
            if day_word == "내일":
                base_date = base_date + timedelta(days=1)

            try:
                candidate = datetime(base_date.year, base_date.month, base_date.day, hour, minute, second)

                if day_word == "내일":
                    return candidate

                # 오늘 or 미지정: 미래면 그대로 반환
                if candidate > base_naive:
                    return candidate

                # 과거라면 오후(+12시간) 시도
                if hour < 12:
                    candidate_pm = candidate.replace(hour=hour + 12)
                    if candidate_pm > base_naive:
                        return candidate_pm

                # "오늘" 명시 → 과거여도 반환 (이후 과거 검증에서 안내)
                if day_word == "오늘":
                    return candidate

                # 날짜 미지정 → 내일로
                return candidate + timedelta(days=1)
            except ValueError:
                pass

    # ── 3단계: dateparser fallback (20분 뒤, 1시간 후 등) ─────────────────
    settings = {
        'RELATIVE_BASE': base_naive,
        'TIMEZONE': 'Asia/Seoul',
        'TO_TIMEZONE': 'Asia/Seoul',
        'PREFER_DATES_FROM': 'future',
        'RETURN_AS_TIMEZONE_AWARE': False,
    }
    return dateparser.parse(text, languages=['ko'], settings=settings)

# [3] 슬래시 명령어: /예약
@bot.tree.command(name="예약", description="지정한 시간에 특정 사용자를 태그하는 알림을 예약합니다.")
@app_commands.describe(
    일시="예: 20분 뒤 / 오늘 오후 3시 / 2026년 6월 2일 8시 10분",
    멘션="태그할 사람을 선택하세요",
    메시지="알림과 함께 보낼 메시지를 적어주세요"
)
async def schedule_notification(
    interaction: discord.Interaction,
    일시: str,
    멘션: discord.Member,
    메시지: str
):
    await interaction.response.defer(ephemeral=True)

    try:
        seoul_tz = pytz.timezone("Asia/Seoul")
        current_time = datetime.now(seoul_tz)

        parsed_date = parse_korean_time(일시, current_time)

        if not parsed_date:
            await interaction.followup.send(
                "❌ 날짜/시간 형식을 인식할 수 없습니다.\n"
                "**올바른 예시:**\n"
                "• `20분 뒤`\n"
                "• `오늘 오후 3시`\n"
                "• `2026년 6월 2일 8시 10분`"
            )
            return

        run_date = seoul_tz.localize(parsed_date.replace(tzinfo=None))

        if run_date < current_time:
            await interaction.followup.send(
                f"❌ 현재 시간보다 이전 시간은 예약할 수 없습니다.\n"
                f"⏰ **현재 한국 시간:** {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"⏳ **입력하신 예약 시간:** {run_date.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return

        # 8자리 ID 생성 + 충돌 방지
        while True:
            job_id = str(uuid.uuid4())[:8]
            if not bot.scheduler.get_job(job_id):
                break

        bot.job_owners[job_id] = interaction.user.id

        bot.scheduler.add_job(
            send_ping,
            'date',
            run_date=run_date,
            args=[interaction.channel_id, 멘션.mention, 메시지, job_id],
            id=job_id,
            name=f"{멘션.display_name} | {메시지}"
        )

        formatted_time = format_korean_time(run_date)

        await interaction.followup.send(
            f"✅ 알림 예약 완료!\n"
            f"🆔 **예약 번호(ID):** `{job_id}`\n"
            f"📅 **일시:** {formatted_time}\n"
            f"👤 **대상:** {멘션.mention}\n"
            f"💬 **내용:** {메시지}"
        )

    except Exception as e:
        print(f"오류 발생: {e}")
        try:
            await interaction.followup.send("❌ 내부 처리 중 오류가 발생했습니다. 다시 시도해 주세요.")
        except:
            pass

# [4] 슬래시 명령어: /예약목록
@bot.tree.command(name="예약목록", description="현재 대기 중인 알림 예약 목록을 보여줍니다.")
async def list_jobs(interaction: discord.Interaction):
    jobs = bot.scheduler.get_jobs()

    if not jobs:
        await interaction.response.send_message("📅 현재 대기 중인 알림 예약을 찾을 수 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(title="⏰ 현재 알림 예약 목록", color=discord.Color.blue())

    for job in jobs:
        try:
            target, msg = job.name.split(" | ", 1)
        except ValueError:
            target, msg = "알 수 없음", "내용 없음"

        if job.next_run_time is None:
            run_time_str = "시간 정보 없음"
        else:
            run_time_str = format_korean_time(job.next_run_time)

        embed.add_field(
            name=f"🆔 번호(ID): `{job.id}`",
            value=f"📅 **일시:** {run_time_str}\n👤 **대상:** {target}\n💬 **내용:** {msg}",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)

# [5] 슬래시 명령어: /예약취소
@bot.tree.command(name="예약취소", description="예약 번호(ID)를 이용해 알림을 취소합니다.")
@app_commands.describe(번호="취소할 예약의 번호(ID)를 입력하세요")
async def cancel_job(interaction: discord.Interaction, 번호: str):
    job = bot.scheduler.get_job(job_id=번호)

    if not job:
        await interaction.response.send_message(
            f"❌ 예약 번호 `{번호}`를 찾을 수 없습니다. 번호를 다시 확인해 주세요.",
            ephemeral=True
        )
        return

    owner_id = bot.job_owners.get(번호)
    is_admin = interaction.user.guild_permissions.administrator

    if owner_id and owner_id != interaction.user.id and not is_admin:
        await interaction.response.send_message(
            f"❌ 예약 번호 `{번호}`는 본인이 만든 예약이 아닙니다.\n"
            f"본인 예약만 취소할 수 있습니다. (관리자는 모든 예약 취소 가능)",
            ephemeral=True
        )
        return

    bot.scheduler.remove_job(job_id=번호)
    bot.job_owners.pop(번호, None)
    await interaction.response.send_message(f"🗑️ 예약 번호 `{번호}` 알림이 성공적으로 취소되었습니다!")

@bot.event
async def on_ready():
    print(f"✅ {bot.user.name} 봇이 로그인 성공했습니다!")

# [6] 봇 실제 구동부
keep_alive()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
bot.run(TOKEN)
