import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import os
import pytz
import uuid
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
import dateparser

# [.env] 파일에 저장된 디스코드 토큰 환경변수 로드
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
    t.start()

# [2] 디스코드 봇 설정
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler.start()
        print("⏰ 스케줄러가 시작되었습니다.")

bot = MyBot()

# 실제 지정된 시간이 되었을 때 실행될 핑 알림 함수
async def send_ping(channel_id, user_mention, message):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.send(f"{user_mention} {message}")
        print(f"🔔 알림 전송 완료: {user_mention}")

# [3] 슬래시 명령어: /예약
@bot.tree.command(name="예약", description="지정한 한국어 시간에 특정 사용자를 태그하는 알림을 예약합니다.")
@app_commands.describe(
    일시="예: 2026년 5월 23일 오후 1시 30분 0초 (또는 '내일 오후 3시', '10분 뒤')",
    멘션="태그할 사람을 선택하세요",
    메시지="알림과 함께 보낼 메시지를 적어주세요"
)
async def schedule_notification(
    interaction: discord.Interaction, 
    일시: str, 
    멘션: discord.Member, 
    메시지: str
):
    # 🚨 [가장 중요] 3초 타임아웃을 피하기 위해 함수가 시작하자마자 아무 조건문 없이 defer를 먼저 실행합니다.
    # 대기 상태를 안전하게 확보하기 위해 ephemeral=True로 설정합니다.
    await interaction.response.defer(ephemeral=True)
    
    try:
        seoul_tz = pytz.timezone("Asia/Seoul")
        current_time = datetime.now(seoul_tz)

        # 시간 해석
        parsed_date = dateparser.parse(
            일시, 
            languages=['ko'], 
            settings={
                'RELATIVE_BASE': current_time.replace(tzinfo=None),
                'TIMEZONE': 'Asia/Seoul',
                'TO_TIMEZONE': 'Asia/Seoul'
            }
        )
        
        # 파싱 실패 시
        if not parsed_date:
            await interaction.followup.send(
                "❌ 날짜/시간 형식을 인식할 수 없습니다.\n"
                "**올바른 예시:**\n"
                "• `2026년 5월 23일 오후 1시 30분`\n"
                "• `오늘 오후 10시`, `10분 뒤`"
            )
            return

        # KST 타임존 확정
        run_date = seoul_tz.localize(parsed_date.replace(tzinfo=None))
        
        # 과거 시간 검증
        if run_date < current_time:
            await interaction.followup.send(
                f"❌ 현재 시간보다 이전 시간은 예약할 수 없습니다.\n"
                f"⏰ **현재 한국 시간:** {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"⏳ **입력하신 예약 시간:** {run_date.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return

        # 4자리 고유 ID 생성
        job_id = str(uuid.uuid4())[:4]

        bot.scheduler.add_job(
            send_ping,
            'date',
            run_date=run_date,
            args=[interaction.channel_id, 멘션.mention, 메시지],
            id=job_id,
            name=f"{멘션.display_name} | {메시지}"
        )
        
        # 시간 포맷팅
        formatted_time = run_date.strftime("%Y년 %m월 %d일 %p %I시 %M분 %S초")
        formatted_time = formatted_time.replace("AM", "오전").replace("PM", "오후")

        # 💡 defer 이후에는 followup.send로 결과 전송
        await interaction.followup.send(
            f"✅ 알림 예약 완료!\n"
            f"🆔 **예약 번호(ID):** {job_id}\n"
            f"📅 **일시:** {formatted_time}\n"
            f"👤 **대상:** {멘션.mention}\n"
            f"💬 **내용:** {메시지}"
        )
        
    except Exception as e:
        print(f"오류 발생: {e}")
        # 만약 에러가 나더라도 디스코드 창에 알림을 남김
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
            
        run_time_str = job.next_run_time.strftime("%Y년 %m월 %d일 %p %I시 %M분 %S초")
        run_time_str = run_time_str.replace("AM", "오전").replace("PM", "오후")
        
        embed.add_field(
            name=f"🆔 번호(ID): {job.id}",
            value=f"📅 **일시:** {run_time_str}\n👤 **대상:** {target}\n💬 **내용:** {msg}",
            inline=False
        )
        
    await interaction.response.send_message(embed=embed)

# [5] 슬래시 명령어: /예약취소
@bot.tree.command(name="예약취소", description="예약 번호(ID)를 이용해 알림을 취소합니다.")
@app_commands.describe(번호="취소할 예약의 4자리 번호(ID)를 입력하세요")
async def cancel_job(interaction: discord.Interaction, 번호: str):
    job = bot.scheduler.get_job(job_id=번호)
    
    if job:
        bot.scheduler.remove_job(job_id=번호)
        await interaction.response.send_message(f"🗑️ 예약 번호 `{번호}` 알림이 성공적으로 취소되었습니다!")
    else:
        await interaction.response.send_message(f"❌ 예약 번호 `{번호}`를 찾을 수 없습니다. 번호를 다시 확인해 주세요.", ephemeral=True)

@bot.event
async def on_ready():
    print(f"✅ {bot.user.name} 봇이 로그인 성공했습니다!")

# [6] 봇 실제 구동부
keep_alive()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
bot.run(TOKEN)