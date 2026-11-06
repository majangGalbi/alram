"""🍽️ /급식 — 나이스(NEIS) 교육정보개방포털 API 로 순천고등학교 급식 메뉴를 보여준다.

master 브랜치의 /급식 을 모듈로 옮긴 것. API 키는 환경변수 NEIS_API_KEY.
학교를 바꾸려면 환경변수 NEIS_ATPT_CODE(시도교육청 코드), NEIS_SCHOOL_CODE(학교 코드)를 지정.
"""
import os
from datetime import datetime
from typing import Optional

import aiohttp
import discord
import pytz
from discord import app_commands

NEIS_BASE_URL = "https://open.neis.go.kr/hub"
DEFAULT_ATPT_CODE = "Q10"        # 전라남도교육청
DEFAULT_SCHOOL_CODE = "8490054"  # 순천고등학교
SCHOOL_NAME = os.getenv("NEIS_SCHOOL_NAME", "순천고등학교")
MEAL_LABELS = {"1": "조식 🌅", "2": "중식 🍱", "3": "석식 🌙"}
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
SEOUL = pytz.timezone("Asia/Seoul")


async def fetch_meal(date_str: str, meal_code: str = "2") -> Optional[dict]:
    """NEIS API 에서 급식 정보를 가져온다. 없으면 None."""
    params = {
        "Type": "json",
        "pIndex": 1,
        "pSize": 10,
        "ATPT_OFCDC_SC_CODE": os.getenv("NEIS_ATPT_CODE", DEFAULT_ATPT_CODE),
        "SD_SCHUL_CODE": os.getenv("NEIS_SCHOOL_CODE", DEFAULT_SCHOOL_CODE),
        "MMEAL_SC_CODE": meal_code,
        "MLSV_YMD": date_str,
    }
    key = os.getenv("NEIS_API_KEY")
    if key:  # 키가 없으면 NEIS 는 샘플 데이터만 주므로 그대로 요청은 하되 키 파라미터는 생략
        params["KEY"] = key
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(f"{NEIS_BASE_URL}/mealServiceDietInfo", params=params) as resp:
            data = await resp.json(content_type=None)
    return parse_meal_response(data)


def parse_meal_response(data) -> Optional[dict]:
    if not isinstance(data, dict) or "mealServiceDietInfo" not in data:
        return None
    try:
        rows = data["mealServiceDietInfo"][1].get("row", [])
    except (IndexError, AttributeError, TypeError):
        return None
    return rows[0] if rows else None


def menu_lines(meal: dict) -> str:
    # DDISH_NM: "메뉴1<br/>메뉴2<br/>..." 형식
    raw_menu = meal.get("DDISH_NM", "") or ""
    items = [item.strip() for item in raw_menu.split("<br/>") if item.strip()]
    return ("\n".join(f"• {item}" for item in items) or "정보 없음")[:1024]


def build_meal_embed(meal: dict, meal_label: str, date_display: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🍽️ {SCHOOL_NAME}  {meal_label}",
        description=f"📅 {date_display}",
        color=discord.Color.orange(),
    )
    embed.add_field(name="📋 메뉴", value=menu_lines(meal), inline=False)
    embed.add_field(name="🔥 칼로리", value=(meal.get("CAL_INFO") or "정보 없음")[:1024], inline=True)
    embed.set_footer(text="출처: 나이스 교육정보개방포털")
    return embed


@app_commands.command(name="급식", description="순천고등학교 오늘의 급식 메뉴를 보여줍니다.")
@app_commands.describe(식사="조식 / 중식 / 석식 (기본값: 중식)")
@app_commands.choices(식사=[
    app_commands.Choice(name="조식", value="1"),
    app_commands.Choice(name="중식", value="2"),
    app_commands.Choice(name="석식", value="3"),
])
async def meal_info(interaction: discord.Interaction, 식사: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer()

    today = datetime.now(SEOUL)
    meal_code = 식사.value if 식사 else "2"
    meal_label = MEAL_LABELS[meal_code]
    date_display = f"{today.year}년 {today.month}월 {today.day}일"

    try:
        meal = await fetch_meal(today.strftime("%Y%m%d"), meal_code)
    except Exception as e:  # 네트워크/JSON 오류
        print(f"급식 API 오류: {type(e).__name__}: {e}")
        await interaction.followup.send("❌ 급식 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
        return

    if not meal:
        await interaction.followup.send(f"📭 오늘({date_display}) {meal_label} 급식 정보가 없습니다.")
        return

    await interaction.followup.send(embed=build_meal_embed(meal, meal_label, date_display))


def register(bot) -> None:
    bot.tree.add_command(meal_info)
