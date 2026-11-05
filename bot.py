import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import os
import pytz
import uuid
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
from time_parser import parse_time, TimeParseError  # 💡 시간 텍스트 파싱 (time_parser.py)
from time_parser import is_recurring, parse_recurrence  # 🔁 반복 예약 표현 파싱

load_dotenv()

# [1] 24시간 호스팅 웹서버 세팅
app = Flask('')

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run():
    # 호스팅 플랫폼(Heroku/Render 등)은 $PORT 로 포트를 지정하므로 이를 우선 사용
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", "8080")))

def keep_alive():
    # daemon=True: 봇이 종료(토큰 오류 등)되면 웹서버 스레드 때문에 프로세스가 좀비로 남지 않도록 함
    t = Thread(target=run, daemon=True)
    t.start()

# [2] 디스코드 봇 설정
MISFIRE_GRACE_SECONDS = 300      # 알림이 지연되어도 5분 이내면 전송
MAX_MESSAGE_LENGTH = 1500        # 디스코드 메시지 2000자 제한(멘션 포함) 안쪽으로 유지
JOB_ID_LENGTH = 4
EMBED_MAX_FIELDS = 25            # 디스코드 임베드 필드 최대 개수
EMBED_FIELD_VALUE_LIMIT = 1024   # 디스코드 임베드 필드 값 최대 길이
MAX_RECURRING_JOBS_PER_SCOPE = 25  # 서버(또는 DM)당 반복 예약 최대 개수 (도배 방지)
SEOUL_TZ = "Asia/Seoul"

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            # 사용자가 입력한 메시지 속 @everyone/@here/역할 멘션이 봇 권한으로 발송되지 않도록 차단
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
        )
        # misfire_grace_time 기본값은 1초라 이벤트 루프가 잠깐만 밀려도 알림이 조용히 누락됨
        self.scheduler = AsyncIOScheduler(
            timezone="Asia/Seoul",
            job_defaults={"misfire_grace_time": MISFIRE_GRACE_SECONDS, "coalesce": True},
        )

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()
        print("⏰ 스케줄러가 시작되었습니다.")

bot = MyBot()

def job_scope(interaction):
    """예약을 구분하는 범위: 서버(길드) 단위, DM이면 채널 단위."""
    return interaction.guild_id or interaction.channel_id


def generate_job_id(scheduler, length=JOB_ID_LENGTH, max_attempts=100):
    """기존 예약과 겹치지 않는 짧은 ID 생성 (겹치면 add_job 이 ConflictingIdError 로 실패함)."""
    for _ in range(max_attempts):
        candidate = uuid.uuid4().hex[:length]
        if scheduler.get_job(candidate) is None:
            return candidate
    # 짧은 ID 공간이 거의 찼다면 더 긴 ID로 대체
    return uuid.uuid4().hex


def format_korean_time(dt):
    """%p 는 로케일에 따라 AM/PM 또는 다른 문자열이 되므로 직접 오전/오후를 붙임."""
    ampm = "오전" if dt.hour < 12 else "오후"
    return dt.strftime(f"%Y년 %m월 %d일 {ampm} %I시 %M분 %S초")


def truncate(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_trigger(recurrence):
    """Recurrence → APScheduler 트리거 (한국 시간 기준). 트리거 객체는 pickle 가능해 영구 저장소와 호환됨."""
    if recurrence.kind == "interval":
        return IntervalTrigger(timezone=SEOUL_TZ, **recurrence.trigger_kwargs())
    return CronTrigger(timezone=SEOUL_TZ, **recurrence.trigger_kwargs())


def count_recurring_jobs(scheduler, scope):
    return sum(1 for job in scheduler.get_jobs()
               if job.kwargs.get("scope") == scope and job.kwargs.get("recurrence"))


# 작업 함수는 모듈 수준 함수, kwargs 는 기본 타입(int/str)만 사용 → SQLAlchemyJobStore 등으로 직렬화 가능
async def send_ping(channel_id, user_id, message, scope=None, target_name=None, recurrence=None):
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            # 캐시에 없는 채널(스레드, DM 등)은 API 로 직접 조회
            channel = await bot.fetch_channel(channel_id)
        await channel.send(
            f"<@{user_id}> {message}",
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=user_id)]),
        )
        print(f"🔔 알림 전송 완료: <@{user_id}>")
    except Exception as e:  # 예약 작업 실패가 조용히 사라지지 않도록 로그를 남김
        print(f"⚠️ 알림 전송 실패 (channel={channel_id}, user={user_id}): {type(e).__name__}: {e}")

# [3] 슬래시 명령어: /예약 (통합 및 한국어 텍스트 입력 방식)
@bot.tree.command(name="예약", description="지정한 한국어 시간에 특정 사용자를 태그하는 알림을 예약합니다.")
@app_commands.describe(
    일시="예: '내일 오후 3시', '10분 뒤', 반복: '매일 오전 7시', '매주 월수금 9시', '2시간마다'",
    멘션="태그할 사람을 선택하세요",
    메시지="알림과 함께 보낼 메시지를 적어주세요"
)
async def schedule_notification(
    interaction: discord.Interaction, 
    일시: str, 
    멘션: discord.Member, 
    메시지: str
):
    await interaction.response.defer(ephemeral=False)
    
    seoul_tz = pytz.timezone("Asia/Seoul")
    current_time = datetime.now(seoul_tz)

    # 🔁 '매일', '매주 월요일', '30분마다' 같은 반복 표현은 반복 예약으로 처리
    if is_recurring(일시):
        await schedule_recurring(interaction, 일시, 멘션, 메시지, current_time)
        return

    # 💡 time_parser 로 한국어/영어/숫자 형식의 시간 텍스트를 datetime 으로 변환합니다.
    #    해석할 수 없거나 모호한 입력은 TimeParseError(사유 포함)로 실패합니다. (README 참고)
    try:
        parsed_date = parse_time(일시, current_time)
    except TimeParseError as e:
        await interaction.followup.send(
            f"❌ 날짜/시간을 인식할 수 없습니다: {e}\n"
            "**올바른 예시:**\n"
            "• `30분 뒤`, `1시간 반 뒤`\n"
            "• `오늘 오후 8시`, `내일 오전 11시 30분`, `모레 아침 7시 반`\n"
            "• `다음주 월요일 9시`, `2026년 11월 5일 오전 9시`\n"
            "• 반복: `매일 오전 7시`, `매주 월수금 9시`, `2시간마다`",
            ephemeral=True
        )
        return

    # 파싱된 시간에 서울 시간대(Timezone) 주입
    run_date = seoul_tz.localize(parsed_date)
    
    # 과거의 시간인지 검증
    if run_date < current_time:
        await interaction.followup.send("❌ 현재 시간보다 이전 시간은 예약할 수 없습니다.", ephemeral=True)
        return

    if len(메시지) > MAX_MESSAGE_LENGTH:
        await interaction.followup.send(f"❌ 메시지는 {MAX_MESSAGE_LENGTH}자 이하로 입력해 주세요.", ephemeral=True)
        return

    # 겹치지 않는 4자리 고유 ID 생성
    job_id = generate_job_id(bot.scheduler)

    bot.scheduler.add_job(
        send_ping,
        'date',
        run_date=run_date,
        kwargs={
            "channel_id": interaction.channel_id,
            "user_id": 멘션.id,
            "message": 메시지,
            "scope": job_scope(interaction),
            "target_name": 멘션.display_name,
        },
        id=job_id,
        name=f"{멘션.display_name} | {메시지}"
    )
    
    # 깔끔하게 포맷팅하여 예약 확인 메시지 전송
    formatted_time = format_korean_time(run_date)

    await interaction.followup.send(
        f"✅ 알림 예약 완료!\n"
        f"🆔 **예약 번호(ID):** {job_id}\n"
        f"📅 **일시:** {formatted_time}\n"
        f"👤 **대상:** {멘션.mention}\n"
        f"💬 **내용:** {메시지}"
    )

async def schedule_recurring(interaction, 일시, 멘션, 메시지, current_time):
    """/예약 의 반복 예약 처리: CronTrigger / IntervalTrigger 로 등록."""
    try:
        recurrence = parse_recurrence(일시, current_time)
    except TimeParseError as e:
        await interaction.followup.send(
            f"❌ 반복 시간을 인식할 수 없습니다: {e}\n"
            "**반복 예약 예시:**\n"
            "• `매일 오전 7시`, `평일 8시 30분`, `주말 오전 10시`\n"
            "• `매주 월요일 9시`, `매주 월수금 오후 6시`, `매주 월~금 9시`\n"
            "• `매월 1일 오전 9시`, `매월 말일 오후 6시`, `매년 12월 25일 오전 9시`\n"
            "• `30분마다`, `2시간마다`, `매시간`, `every day 7am`",
            ephemeral=True
        )
        return

    if len(메시지) > MAX_MESSAGE_LENGTH:
        await interaction.followup.send(f"❌ 메시지는 {MAX_MESSAGE_LENGTH}자 이하로 입력해 주세요.", ephemeral=True)
        return

    scope = job_scope(interaction)
    if count_recurring_jobs(bot.scheduler, scope) >= MAX_RECURRING_JOBS_PER_SCOPE:
        await interaction.followup.send(
            f"❌ 반복 예약은 서버당 최대 {MAX_RECURRING_JOBS_PER_SCOPE}개까지 등록할 수 있습니다. "
            "`/예약목록`에서 확인 후 `/예약취소`로 정리해 주세요.",
            ephemeral=True
        )
        return

    trigger = build_trigger(recurrence)
    next_run = trigger.get_next_fire_time(None, current_time)
    if next_run is None:
        await interaction.followup.send("❌ 앞으로 실행될 시각이 없는 반복 규칙입니다. 다시 확인해 주세요.", ephemeral=True)
        return

    job_id = generate_job_id(bot.scheduler)
    bot.scheduler.add_job(
        send_ping,
        trigger=trigger,
        kwargs={
            "channel_id": interaction.channel_id,
            "user_id": 멘션.id,
            "message": 메시지,
            "scope": scope,
            "target_name": 멘션.display_name,
            "recurrence": recurrence.description,
        },
        id=job_id,
        name=f"{멘션.display_name} | {메시지}"
    )

    await interaction.followup.send(
        f"✅ 반복 알림 예약 완료!\n"
        f"🆔 **예약 번호(ID):** {job_id}\n"
        f"🔁 **반복:** {recurrence.description}\n"
        f"⏭️ **다음 알림:** {format_korean_time(next_run)}\n"
        f"👤 **대상:** {멘션.mention}\n"
        f"💬 **내용:** {메시지}\n"
        f"(그만 받으려면 `/예약취소 {job_id}`)"
    )

# [4] 슬래시 명령어: /예약목록
@bot.tree.command(name="예약목록", description="현재 대기 중인 알림 예약 목록을 보여줍니다.")
async def list_jobs(interaction: discord.Interaction):
    # 다른 서버의 예약(대상/내용)이 노출되지 않도록 현재 서버의 예약만 표시
    scope = job_scope(interaction)
    jobs = [job for job in bot.scheduler.get_jobs() if job.kwargs.get("scope") == scope]
    
    if not jobs:
        await interaction.response.send_message("📅 현재 대기 중인 알림 예약을 찾을 수 없습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(title="⏰ 현재 알림 예약 목록", color=discord.Color.blue())
    
    shown = jobs[:EMBED_MAX_FIELDS]
    for job in shown:
        user_id = job.kwargs.get("user_id")
        target = f"<@{user_id}>" if user_id else "알 수 없음"
        msg = job.kwargs.get("message") or "내용 없음"

        run_time_str = format_korean_time(job.next_run_time) if job.next_run_time else "일시정지됨"
        recurrence = job.kwargs.get("recurrence")
        
        if recurrence:
            value = (f"🔁 **반복:** {recurrence}\n⏭️ **다음 알림:** {run_time_str}\n"
                     f"👤 **대상:** {target}\n💬 **내용:** {msg}")
        else:
            value = f"📅 **일시:** {run_time_str}\n👤 **대상:** {target}\n💬 **내용:** {msg}"
        embed.add_field(
            name=f"🆔 번호(ID): {job.id}",
            value=truncate(value, EMBED_FIELD_VALUE_LIMIT),
            inline=False
        )

    if len(jobs) > len(shown):
        embed.set_footer(text=f"외 {len(jobs) - len(shown)}건의 예약이 더 있습니다.")
        
    await interaction.response.send_message(embed=embed)

# [5] 슬래시 명령어: /예약취소
@bot.tree.command(name="예약취소", description="예약 번호(ID)를 이용해 알림을 취소합니다.")
@app_commands.describe(번호="취소할 예약의 4자리 번호(ID)를 입력하세요")
async def cancel_job(interaction: discord.Interaction, 번호: str):
    번호 = 번호.strip().lower()
    job = bot.scheduler.get_job(job_id=번호)
    
    # 다른 서버의 예약은 번호를 알아도 취소할 수 없도록 범위 확인
    if job and job.kwargs.get("scope") == job_scope(interaction):
        kind = "반복 알림" if job.kwargs.get("recurrence") else "알림"
        job.remove()
        await interaction.response.send_message(f"🗑️ 예약 번호 `{번호}` {kind}이 성공적으로 취소되었습니다!")
    else:
        await interaction.response.send_message(f"❌ 예약 번호 `{번호}`를 찾을 수 없습니다. 번호를 다시 확인해 주세요.", ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """처리되지 않은 예외로 응답이 '생각 중...' 상태에 멈추지 않도록 사용자에게 오류를 알림."""
    print(f"⚠️ 명령어 처리 중 오류: {type(error).__name__}: {error}")
    text = "❌ 명령을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    print(f"✅ {bot.user.name} 봇이 로그인 성공했습니다!")

# [6] 봇 실제 구동부
def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("❌ DISCORD_BOT_TOKEN 환경변수가 설정되지 않았습니다.")
    keep_alive()
    bot.run(token)


if __name__ == "__main__":
    main()
