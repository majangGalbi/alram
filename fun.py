"""뽑기 도구 (/주사위, /뽑기, /팀나누기).

bot.py 에서는 ``fun.register(bot)`` 한 줄로만 연결한다.
난수는 모두 ``random.SystemRandom`` (OS 엔트로피, secrets 와 동일 소스) 을 사용한다.

필요 인텐트: /팀나누기 는 음성 채널 멤버를 캐시에서 읽으므로 ``voice_states`` 인텐트가
필요하다. discord.Intents.default() 에 포함되어 있고 특권(privileged) 인텐트가 아니라
개발자 포털에서 따로 켤 필요는 없다.
"""
import random
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

import discord
from discord import app_commands

RNG = random.SystemRandom()

DICE_EXPR_MAX_LEN = 30
DICE_MAX_COUNT = 100
DICE_MAX_SIDES = 1000
DICE_MAX_MODIFIER = 100000
DEFAULT_DICE = "1d6"

PICK_INPUT_MAX_LEN = 1000
PICK_MAX_ITEMS = 100
PICK_ITEM_MAX_LEN = 100

TEAM_MIN = 2
TEAM_MAX = 10

NO_PING = discord.AllowedMentions.none()

_DICE_RE = re.compile(r"^(\d*)\s*[dD]\s*(\d+)\s*(?:([+-])\s*(\d+))?$", re.ASCII)  # 전각·타 문자권 숫자 거부
_SPLIT_RE = re.compile(r"[,，、\n]")


class InputError(ValueError):
    """사용자에게 그대로 보여줄 입력 오류."""


@dataclass(frozen=True)
class DiceSpec:
    count: int
    sides: int
    modifier: int = 0

    def __str__(self) -> str:
        mod = f"{self.modifier:+d}" if self.modifier else ""
        return f"{self.count}d{self.sides}{mod}"


def parse_dice(expr: Optional[str]) -> DiceSpec:
    """"2d6+3", "d20", "3D8-1" 형식 파싱. 개수·면·보정값 상한을 검사한다."""
    expr = (expr or DEFAULT_DICE).strip()
    if not expr:
        expr = DEFAULT_DICE
    if len(expr) > DICE_EXPR_MAX_LEN:
        raise InputError(f"표현식이 너무 깁니다. ({DICE_EXPR_MAX_LEN}자 이하)")
    m = _DICE_RE.match(expr)
    if not m:
        raise InputError("형식이 올바르지 않습니다. 예: `1d6`, `2d6+3`, `d20`, `3d8-1`")
    count = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    modifier = int(m.group(4)) if m.group(4) else 0
    if m.group(3) == "-":
        modifier = -modifier
    if not 1 <= count <= DICE_MAX_COUNT:
        raise InputError(f"주사위 개수는 1~{DICE_MAX_COUNT}개여야 합니다.")
    if not 2 <= sides <= DICE_MAX_SIDES:
        raise InputError(f"주사위 면 수는 2~{DICE_MAX_SIDES}이어야 합니다.")
    if abs(modifier) > DICE_MAX_MODIFIER:
        raise InputError(f"보정값은 ±{DICE_MAX_MODIFIER} 이내여야 합니다.")
    return DiceSpec(count, sides, modifier)


def roll_dice(spec: DiceSpec, rng=RNG) -> List[int]:
    return [rng.randint(1, spec.sides) for _ in range(spec.count)]


def parse_items(text: Optional[str]) -> List[str]:
    """쉼표(또는 줄바꿈)로 구분된 항목. 공백 제거, 빈 항목·중복 제거(순서 유지)."""
    text = text or ""
    if len(text) > PICK_INPUT_MAX_LEN:
        raise InputError(f"입력이 너무 깁니다. ({PICK_INPUT_MAX_LEN}자 이하)")
    items: List[str] = []
    seen = set()
    for raw in _SPLIT_RE.split(text):
        item = raw.strip()
        if not item or item in seen:
            continue
        if len(item) > PICK_ITEM_MAX_LEN:
            raise InputError(f"각 항목은 {PICK_ITEM_MAX_LEN}자 이하여야 합니다.")
        seen.add(item)
        items.append(item)
    if len(items) > PICK_MAX_ITEMS:
        raise InputError(f"항목은 최대 {PICK_MAX_ITEMS}개까지 가능합니다.")
    if len(items) < 1:
        raise InputError('항목을 쉼표로 구분해 입력해 주세요. 예: `"짜장, 짬뽕, 볶음밥"`')
    return items


def pick(items: Sequence[str], count: int = 1, rng=RNG) -> List[str]:
    if not 1 <= count <= len(items):
        raise InputError(f"뽑을 개수는 1~{len(items)}개여야 합니다.")
    return rng.sample(list(items), count)


def split_teams(members: Sequence, team_count: int, rng=RNG) -> List[list]:
    """무작위로 섞은 뒤 순서대로 배분 → 팀 인원 차이는 최대 1명."""
    if team_count < 1:
        raise InputError("팀 수는 1 이상이어야 합니다.")
    if len(members) < team_count:
        raise InputError(f"인원({len(members)}명)이 팀 수({team_count})보다 적습니다.")
    shuffled = list(members)
    rng.shuffle(shuffled)
    teams: List[list] = [[] for _ in range(team_count)]
    for i, m in enumerate(shuffled):
        teams[i % team_count].append(m)
    return teams


def voice_members(user) -> Optional[list]:
    """호출자가 있는 음성 채널의 사람 멤버 목록. 음성 채널에 없으면 None."""
    voice = getattr(user, "voice", None)
    channel = getattr(voice, "channel", None) if voice else None
    if channel is None:
        return None
    return [m for m in channel.members if not getattr(m, "bot", False)]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ───────────────────── 명령 처리 (테스트에서 직접 호출) ─────────────────────

async def _error(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(f"❌ {text}", ephemeral=True, allowed_mentions=NO_PING)
    else:
        await interaction.response.send_message(f"❌ {text}", ephemeral=True, allowed_mentions=NO_PING)


async def handle_dice(interaction: discord.Interaction, expr: Optional[str] = None, rng=RNG) -> None:
    try:
        spec = parse_dice(expr)
    except InputError as e:
        await _error(interaction, str(e))
        return
    rolls = roll_dice(spec, rng)
    total = sum(rolls) + spec.modifier
    detail = ", ".join(map(str, rolls))
    if spec.modifier:
        detail = f"[{detail}] {spec.modifier:+d}"
    text = f"🎲 **{spec}** → **{total}**"
    if spec.count > 1 or spec.modifier:
        text += f"\n({_clip(detail, 1500)})"
    await interaction.response.send_message(text, allowed_mentions=NO_PING)


async def handle_pick(interaction: discord.Interaction, text: str, count: int = 1, rng=RNG) -> None:
    try:
        items = parse_items(text)
        chosen = pick(items, count, rng)
    except InputError as e:
        await _error(interaction, str(e))
        return
    shown = ", ".join(f"**{discord.utils.escape_markdown(c)}**" for c in chosen)
    msg = f"🎯 {len(items)}개 중 {len(chosen)}개 뽑기 결과: {shown}"
    await interaction.response.send_message(_clip(msg, 1900), allowed_mentions=NO_PING)


async def handle_teams(interaction: discord.Interaction, team_count: int = 2, rng=RNG) -> None:
    if interaction.guild is None:
        await _error(interaction, "팀 나누기는 서버에서만 사용할 수 있습니다.")
        return
    if not TEAM_MIN <= team_count <= TEAM_MAX:
        await _error(interaction, f"팀 수는 {TEAM_MIN}~{TEAM_MAX}이어야 합니다.")
        return
    members = voice_members(interaction.user)
    if members is None:
        await _error(interaction, "먼저 음성 채널에 들어간 뒤 다시 사용해 주세요.")
        return
    try:
        teams = split_teams(members, team_count, rng)
    except InputError as e:
        await _error(interaction, str(e))
        return
    channel_name = discord.utils.escape_markdown(interaction.user.voice.channel.name)
    embed = discord.Embed(title=f"👥 팀 나누기 ({channel_name}, {len(members)}명 → {team_count}팀)",
                          color=discord.Color.blurple())
    for i, team in enumerate(teams, 1):
        value = "\n".join(m.mention for m in team)
        embed.add_field(name=f"{i}팀 ({len(team)}명)", value=_clip(value, 1024))
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_PING)


def register(bot) -> None:
    """봇의 명령 트리에 뽑기 도구 명령을 등록한다."""

    @app_commands.command(name="주사위", description="주사위를 굴립니다. 예: 1d6, 2d6+3, d20")
    @app_commands.describe(표현식="NdM+K 형식 (기본 1d6, 최대 100개·1000면)")
    async def dice_cmd(interaction: discord.Interaction,
                       표현식: Optional[app_commands.Range[str, 1, DICE_EXPR_MAX_LEN]] = None):
        await handle_dice(interaction, 표현식)

    @app_commands.command(name="뽑기", description="쉼표로 구분한 항목 중 무작위로 뽑습니다.")
    @app_commands.describe(항목='쉼표로 구분 (예: "짜장, 짬뽕, 볶음밥")', 개수="뽑을 개수 (기본 1)")
    async def pick_cmd(interaction: discord.Interaction,
                       항목: app_commands.Range[str, 1, PICK_INPUT_MAX_LEN],
                       개수: app_commands.Range[int, 1, PICK_MAX_ITEMS] = 1):
        await handle_pick(interaction, 항목, 개수)

    @app_commands.command(name="팀나누기", description="내가 있는 음성 채널 멤버(봇 제외)를 무작위로 팀 분배합니다.")
    @app_commands.describe(팀수=f"팀 수 (기본 2, {TEAM_MIN}~{TEAM_MAX})")
    @app_commands.guild_only()
    async def teams_cmd(interaction: discord.Interaction,
                        팀수: app_commands.Range[int, TEAM_MIN, TEAM_MAX] = 2):
        await handle_teams(interaction, 팀수)

    for cmd in (dice_cmd, pick_cmd, teams_cmd):
        bot.tree.add_command(cmd)
    intents = getattr(bot, "intents", None)
    if intents is not None and not intents.voice_states:
        print("⚠️ voice_states 인텐트가 꺼져 있어 /팀나누기 가 음성 채널 멤버를 찾지 못할 수 있습니다.")
