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
        # 스케줄러 자체에 한국 시간대 강제 설정
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
    # 3초 제한 우회를 위해 디스코드 응답 연장
    await interaction.response.defer(ephemeral=False)
    
    seoul_tz = pytz.timezone("Asia/Seoul")
    current_time = datetime.now(seoul_tz)

    # 해외 서버(Render) 시차 문제를 해결하기 위해 TIMEZONE 설정을 확실히 주입
    parsed_date = dateparser.parse(
        일시, 
        languages=['ko'], 
        settings={
            'RELATIVE_BASE': current_time.replace(tzinfo=None),
            'TIMEZONE': 'Asia/Seoul',  # 입력된 텍스트를 한국 시간으로 해석
            'TO_TIMEZONE': 'Asia/Seoul' # 결과물도 한국 시간으로 고정
        }
    )
    
    # 파싱에 실패한 경우
    if not parsed_date:
        await interaction.followup.send(
            "❌ 날짜/시간 형식을 인식할 수 없습니다.\n"
            "**올바른 예시:**\n"
            "• `2026년 5월 23일 오후 1시 30분 0초`\n"
            "• `오늘 오후 10시`, `10분 뒤`, `내일 오전 9시`", 
            ephemeral=True
        )
        return

    # 타임존 정보가 꼬이지 않도록 KST 타임존 확정 주입
    run_date = seoul_tz.localize(parsed_date.replace(tzinfo=None))
    
    # 과거의 시간인지 검증
    if run_date < current_time:
        await interaction.followup.send(
            f"❌ 현재 시간보다 이전 시간은 예약할 수 없습니다.\n"
            f"⏰ **봇이 인식한 현재 한국 시간:** {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"⏳ **입력하신 예약 시간:** {run_date.strftime('%Y-%m-%d %H:%M:%S')}", 
            ephemeral=True
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
    
    # 깔끔하게 포맷팅하여 예약 확인 메시지 전송
    formatted_time = run_date.strftime("%Y년 %m월 %d일 %p %I시 %M분 %S초")
    formatted_time = formatted_time.replace("AM", "오전").replace("PM", "오후")

    await interaction.followup.send(
        f"✅ 알림 예약 완료!\n"
        f"🆔 **예약 번호(ID):** {job_id}\n"
        f"📅 **일시:** {formatted_time}\n"
        f"👤 **대상:** {멘션.mention}\n"
        f"💬 **내용:** {메시지}"
    )

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

# .env에서 불러온 토큰으로 안전하게 로그인
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
bot.run(TOKEN)