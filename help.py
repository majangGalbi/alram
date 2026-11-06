"""📖 /도움말 슬래시 명령 + 웹 명령 목록(/help) 페이지.

명령 목록은 하드코딩하지 않고 bot.tree.get_commands() 에서 이름·설명·파라미터를 읽어 자동 생성한다.
카테고리와 예시만 아래 매핑 테이블로 보강하며, 매핑에 없는 새 명령은 자동으로 "기타" 로 분류된다.
웹 페이지에는 명령 설명만 렌더링하며 서버/사용자 데이터는 일절 포함하지 않는다.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import discord
from discord import app_commands

OTHER_CATEGORY = "기타"

# 카테고리 순서 = 표시 순서. (이모지, 짧은 소개)
CATEGORIES: Dict[str, tuple] = {
    "예약 알림": ("⏰", "지정한 시각(또는 반복 주기)에 사람을 태그해 알려 줍니다."),
    "투표": ("🗳️", "버튼으로 참여하는 투표를 만들고 마감합니다."),
    "출석·랭킹": ("✅", "매일 출석체크로 포인트를 모으고 순위를 확인합니다."),
    "뽑기 도구": ("🎲", "주사위·무작위 뽑기·팀 나누기."),
    "달력 일정표": ("📅", "서버 공용 일정 등록·조회·달력·일괄 등록/내보내기."),
    "레벨": ("💬", "채팅으로 경험치를 모아 레벨을 올리고 순위를 확인합니다."),
    "학교 급식": ("🍽️", "나이스 급식 메뉴 조회."),
    "도움말": ("📖", "명령 사용법 안내."),
    OTHER_CATEGORY: ("🧩", "그 밖의 명령."),
}

# 명령 이름 → 카테고리. 여기에 없는 명령은 OTHER_CATEGORY.
COMMAND_CATEGORIES: Dict[str, str] = {
    "예약": "예약 알림", "예약목록": "예약 알림", "예약취소": "예약 알림",
    "투표": "투표", "투표마감": "투표",
    "출석": "출석·랭킹", "출석현황": "출석·랭킹", "랭킹": "출석·랭킹",
    "주사위": "뽑기 도구", "뽑기": "뽑기 도구", "팀나누기": "뽑기 도구",
    "일정추가": "달력 일정표", "일정목록": "달력 일정표", "달력": "달력 일정표", "일정수정": "달력 일정표",
    "일정삭제": "달력 일정표", "일정일괄추가": "달력 일정표", "일정내보내기": "달력 일정표",
    "레벨": "레벨", "레벨랭킹": "레벨", "레벨설정": "레벨", "레벨보상": "레벨",
    "급식": "학교 급식",
    "도움말": "도움말",
}

# 명령 이름 → 사용 예시 (선택). 없으면 예시 줄을 생략한다.
COMMAND_EXAMPLES: Dict[str, List[str]] = {
    "예약": ["/예약 일시:내일 오후 3시 멘션:@친구 메시지:회의 시작", "/예약 일시:매주 월수금 9시 멘션:@나 메시지:운동"],
    "예약목록": ["/예약목록"],
    "예약취소": ["/예약취소 번호:a1b2"],
    "투표": ["/투표 질문:점심 메뉴? 항목:짜장, 짬뽕, 볶음밥 마감:30분 뒤"],
    "투표마감": ["/투표마감 메시지id:123456789012345678"],
    "출석": ["/출석"],
    "출석현황": ["/출석현황", "/출석현황 사용자:@친구"],
    "랭킹": ["/랭킹"],
    "주사위": ["/주사위", "/주사위 표현식:2d6+3"],
    "뽑기": ["/뽑기 항목:짜장, 짬뽕, 볶음밥", "/뽑기 항목:a, b, c, d 개수:2"],
    "팀나누기": ["/팀나누기 팀수:3"],
    "일정추가": ["/일정추가 제목:정기 회의 일시:다음주 금요일 오후 7시"],
    "일정목록": ["/일정목록", "/일정목록 기간:이번달"],
    "달력": ["/달력", "/달력 년:2026 월:12"],
    "일정수정": ["/일정수정 번호:3 일시:금요일 오후 8시"],
    "일정삭제": ["/일정삭제 번호:3"],
    "일정일괄추가": ["/일정일괄추가 파일:(일정.txt 첨부)", "/일정일괄추가  ← 파일 없이 실행하면 입력창"],
    "일정내보내기": ["/일정내보내기 기간:이번달"],
    "도움말": ["/도움말", "/도움말 카테고리:투표"],
}

# 봇 초대 시 필요한 권한(이름, 비트값). 합계가 초대 링크의 permissions 값.
REQUIRED_PERMISSIONS = [
    ("채널 보기 (View Channels)", 1 << 10),
    ("메시지 보내기 (Send Messages)", 1 << 11),
    ("링크 첨부 (Embed Links)", 1 << 14),
    ("파일 첨부 (Attach Files)", 1 << 15),
    ("메시지 기록 보기 (Read Message History)", 1 << 16),
]
INVITE_PERMISSIONS = sum(v for _, v in REQUIRED_PERMISSIONS)  # 117760
INVITE_SCOPES = ("bot", "applications.commands")
REQUIRED_INTENTS = [
    ("MESSAGE CONTENT (특권)", "개발자 포털 → Bot → Privileged Gateway Intents 에서 켜야 함 (bot.py 가 요청)"),
    ("voice_states", "기본 인텐트에 포함 — /팀나누기 음성 채널 멤버 확인용, 포털 설정 불필요"),
    ("SERVER MEMBERS (특권)", "필요 없음 (/랭킹 에서 캐시에 없는 멤버는 <@아이디> 로 표시)"),
]

# 디스코드 임베드 제한
EMBED_MAX_FIELDS = 25
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_FIELD_NAME_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_TOTAL_LIMIT = 6000
SELECT_MAX_OPTIONS = 25


@dataclass
class ParamInfo:
    name: str
    description: str
    required: bool
    choices: List[str] = field(default_factory=list)


@dataclass
class CommandInfo:
    name: str  # 하위 명령이면 "그룹 하위" 형태
    description: str
    params: List[ParamInfo]
    category: str
    examples: List[str]

    @property
    def usage(self) -> str:
        parts = [f"/{self.name}"]
        for p in self.params:
            parts.append(f"<{p.name}>" if p.required else f"[{p.name}]")
        return " ".join(parts)


def _leaf_commands(cmd):
    """그룹이면 하위 명령까지 펼친다 (컨텍스트 메뉴 등 파라미터 없는 객체도 안전하게 처리)."""
    if isinstance(cmd, app_commands.Group):
        for sub in cmd.commands:
            yield from _leaf_commands(sub)
    else:
        yield cmd


def _category_for(name: str) -> str:
    top = name.split(" ", 1)[0]
    cat = COMMAND_CATEGORIES.get(name) or COMMAND_CATEGORIES.get(top)
    return cat if cat in CATEGORIES else OTHER_CATEGORY


def collect_commands(bot) -> List[CommandInfo]:
    """bot.tree 에 등록된 전역 슬래시 명령을 읽어 CommandInfo 목록으로 만든다."""
    infos: List[CommandInfo] = []
    for top in bot.tree.get_commands():
        for cmd in _leaf_commands(top):
            name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", "?")
            params = []
            for p in getattr(cmd, "parameters", []) or []:
                params.append(ParamInfo(
                    name=getattr(p, "display_name", None) or p.name,
                    description=p.description or "",
                    required=bool(p.required),
                    choices=[c.name for c in (getattr(p, "choices", None) or [])],
                ))
            desc = getattr(cmd, "description", "") or ""
            if desc == "…":  # discord.py 가 설명이 없을 때 넣는 기본값
                desc = ""
            infos.append(CommandInfo(name, desc, params, _category_for(name), list(COMMAND_EXAMPLES.get(name, []))))
    return infos


def group_by_category(infos: List[CommandInfo]) -> Dict[str, List[CommandInfo]]:
    """CATEGORIES 순서대로, 명령이 있는 카테고리만 담는다."""
    grouped: Dict[str, List[CommandInfo]] = {c: [] for c in CATEGORIES}
    for info in infos:
        grouped.setdefault(info.category, []).append(info)
    return {c: cmds for c, cmds in grouped.items() if cmds}


def invite_url(client_id) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={client_id}"
            f"&permissions={INVITE_PERMISSIONS}&scope={'+'.join(INVITE_SCOPES)}")


# ---------------------------------------------------------------------------
# 임베드
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field_value(info: CommandInfo) -> str:
    lines = [info.description or "(설명 없음)", f"`{info.usage}`"]
    for p in info.params:
        extra = f" (선택지: {', '.join(p.choices)})" if p.choices else ""
        lines.append(f"• `{p.name}`{'' if p.required else ' (선택)'}: {p.description}{extra}")
    for ex in info.examples:
        lines.append(f"예) `{ex}`")
    return _clip("\n".join(lines), EMBED_FIELD_VALUE_LIMIT)


def _embed_len(embed: discord.Embed) -> int:
    return len(embed)


def build_overview_embed(grouped: Dict[str, List[CommandInfo]]) -> discord.Embed:
    embed = discord.Embed(
        title="📖 도움말",
        description="아래 드롭다운에서 카테고리를 고르면 명령별 설명과 예시를 볼 수 있어요.\n"
                    "`<값>` 은 필수, `[값]` 은 선택 입력입니다.",
        color=discord.Color.blurple(),
    )
    for cat, cmds in list(grouped.items())[:EMBED_MAX_FIELDS]:
        emoji, intro = CATEGORIES.get(cat, CATEGORIES[OTHER_CATEGORY])
        names = " ".join(f"`/{c.name}`" for c in cmds)
        embed.add_field(name=f"{emoji} {cat}", value=_clip(f"{intro}\n{names}", EMBED_FIELD_VALUE_LIMIT), inline=False)
    return embed


def build_category_embed(category: str, cmds: List[CommandInfo]) -> discord.Embed:
    """카테고리 임베드. 필드 25개·필드 1024자·총 6000자 제한을 지킨다 (넘치면 뒤 명령은 이름만 안내)."""
    emoji, intro = CATEGORIES.get(category, CATEGORIES[OTHER_CATEGORY])
    embed = discord.Embed(title=f"{emoji} {category}", description=intro, color=discord.Color.blurple())
    footer = "드롭다운으로 다른 카테고리를 볼 수 있어요."
    embed.set_footer(text=footer)
    reserve = 200  # '생략됨' 안내 필드용 여유
    omitted: List[str] = []
    for info in cmds:
        name = _clip(f"/{info.name}", EMBED_FIELD_NAME_LIMIT)
        value = _field_value(info)
        too_many = len(embed.fields) >= EMBED_MAX_FIELDS - 1
        too_long = _embed_len(embed) + len(name) + len(value) > EMBED_TOTAL_LIMIT - reserve
        if omitted or too_many or too_long:
            omitted.append(f"/{info.name}")
            continue
        embed.add_field(name=name, value=value, inline=False)
    if omitted:
        embed.add_field(name="➕ 더 있는 명령", value=_clip(" ".join(omitted), min(EMBED_FIELD_VALUE_LIMIT, 150)),
                        inline=False)
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self, grouped: Dict[str, List[CommandInfo]], current: Optional[str] = None):
        options = [discord.SelectOption(label="전체 목록", value="", emoji="📖", default=current is None)]
        for cat, cmds in list(grouped.items())[: SELECT_MAX_OPTIONS - 1]:
            emoji = CATEGORIES.get(cat, CATEGORIES[OTHER_CATEGORY])[0]
            options.append(discord.SelectOption(label=cat, value=cat, emoji=emoji, default=cat == current,
                                                description=_clip(f"{len(cmds)}개 명령", 100)))
        super().__init__(placeholder="카테고리 선택", options=options, min_values=1, max_values=1)
        self.grouped = grouped

    async def callback(self, interaction: discord.Interaction):
        cat = self.values[0] or None
        embed = build_overview_embed(self.grouped) if cat is None else build_category_embed(cat, self.grouped[cat])
        await interaction.response.edit_message(embed=embed, view=HelpView(self.grouped, cat))


class HelpView(discord.ui.View):
    def __init__(self, grouped: Dict[str, List[CommandInfo]], current: Optional[str] = None):
        super().__init__(timeout=300)
        self.add_item(HelpSelect(grouped, current))


def resolve_category(query: Optional[str], grouped: Dict[str, List[CommandInfo]]) -> Optional[str]:
    """입력한 카테고리 이름(또는 명령 이름)을 실제 카테고리로. 없으면 None (= 전체 목록)."""
    if not query:
        return None
    q = query.strip().lstrip("/").replace(" ", "")
    for cat in grouped:
        if q and (q == cat.replace(" ", "") or q in cat.replace(" ", "")):
            return cat
    for cat, cmds in grouped.items():
        if any(c.name.replace(" ", "") == q for c in cmds):
            return cat
    return None


async def handle_help(interaction: discord.Interaction, bot, category: Optional[str] = None):
    grouped = group_by_category(collect_commands(bot))
    cat = resolve_category(category, grouped)
    embed = build_overview_embed(grouped) if cat is None else build_category_embed(cat, grouped[cat])
    await interaction.response.send_message(embed=embed, view=HelpView(grouped, cat), ephemeral=True)


def register(bot) -> None:
    """/도움말 명령을 bot.tree 에 등록한다."""

    @app_commands.command(name="도움말", description="봇 명령 목록과 사용법·예시를 보여줍니다. (본인에게만 보임)")
    @app_commands.describe(카테고리="볼 카테고리 또는 명령 이름 (비우면 전체 목록)")
    async def help_cmd(interaction: discord.Interaction, 카테고리: Optional[str] = None):
        await handle_help(interaction, bot, 카테고리)

    @help_cmd.autocomplete("카테고리")
    async def _category_autocomplete(interaction: discord.Interaction, current: str):
        cats = list(group_by_category(collect_commands(bot)))
        return [app_commands.Choice(name=c, value=c) for c in cats if current in c][:25]

    bot.tree.add_command(help_cmd)


# ---------------------------------------------------------------------------
# 웹 페이지 (/help, /commands)
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#f7f7f9;--card:#fff;--fg:#1f2328;--muted:#5b6370;--accent:#5865f2;--code:#eef0f4;--border:#e3e5ea}
@media (prefers-color-scheme:dark){:root{--bg:#1e1f22;--card:#2b2d31;--fg:#e8e9eb;--muted:#a4a9b3;
--accent:#8b95ff;--code:#383a40;--border:#3a3c42}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo","Noto Sans KR",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}h1{margin:0 0 4px}h2{margin:32px 0 12px;
padding-top:8px;border-top:1px solid var(--border)}.muted{color:var(--muted)}a{color:var(--accent)}
nav ul{display:flex;flex-wrap:wrap;gap:8px;padding:0;list-style:none}nav a{display:inline-block;padding:4px 12px;
border:1px solid var(--border);border-radius:999px;text-decoration:none;background:var(--card)}
.cmd{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin:12px 0}
.cmd h3{margin:0 0 4px;font-size:1.05em}code{background:var(--code);border-radius:4px;padding:1px 5px;
font-size:.92em;word-break:break-all}ul.params{margin:6px 0;padding-left:20px}table{border-collapse:collapse;width:100%}
td,th{border:1px solid var(--border);padding:6px 8px;text-align:left;vertical-align:top}
"""


def _e(text) -> str:
    return html.escape(str(text), quote=True)


def _anchor(cat: str) -> str:
    return "cat-" + str(list(CATEGORIES).index(cat) if cat in CATEGORIES else abs(hash(cat)) % 10000)


def render_help_html(bot) -> str:
    """명령 설명만으로 HTML 페이지를 만든다 (서버/사용자 데이터 미포함)."""
    grouped = group_by_category(collect_commands(bot))
    out = ['<!doctype html><html lang="ko"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<meta name="color-scheme" content="light dark">',
           f"<title>봇 명령 도움말</title><style>{_CSS}</style></head><body><main>",
           "<h1>📖 봇 명령 도움말</h1>",
           '<p class="muted">디스코드에서 <code>/도움말</code> 로도 볼 수 있어요. '
           "<code>&lt;값&gt;</code> 은 필수, <code>[값]</code> 은 선택 입력입니다.</p>",
           "<nav><ul>"]
    for cat, cmds in grouped.items():
        emoji = CATEGORIES.get(cat, CATEGORIES[OTHER_CATEGORY])[0]
        out.append(f'<li><a href="#{_anchor(cat)}">{_e(emoji)} {_e(cat)} ({len(cmds)})</a></li>')
    out.append('<li><a href="#invite">🔗 봇 초대</a></li></ul></nav>')
    for cat, cmds in grouped.items():
        emoji, intro = CATEGORIES.get(cat, CATEGORIES[OTHER_CATEGORY])
        out.append(f'<section id="{_anchor(cat)}"><h2>{_e(emoji)} {_e(cat)}</h2><p class="muted">{_e(intro)}</p>')
        for info in cmds:
            out.append(f'<div class="cmd"><h3><code>/{_e(info.name)}</code></h3>')
            out.append(f"<div>{_e(info.description or '(설명 없음)')}</div>")
            out.append(f'<div class="muted">사용법: <code>{_e(info.usage)}</code></div>')
            if info.params:
                out.append('<ul class="params">')
                for p in info.params:
                    choices = f" (선택지: {_e(', '.join(p.choices))})" if p.choices else ""
                    opt = "" if p.required else " <span class=\"muted\">(선택)</span>"
                    out.append(f"<li><code>{_e(p.name)}</code>{opt}: {_e(p.description)}{choices}</li>")
                out.append("</ul>")
            for ex in info.examples:
                out.append(f'<div>예) <code>{_e(ex)}</code></div>')
            out.append("</div>")
        out.append("</section>")
    out.append('<section id="invite"><h2>🔗 봇 초대 링크 만들기</h2>')
    out.append("<p>아래 주소에서 <code>CLIENT_ID</code> 를 Discord Developer Portal → 애플리케이션 → "
               "<b>General Information</b> 의 <b>Application ID</b> 로 바꾸세요.</p>")
    out.append(f"<p><code>{_e(invite_url('CLIENT_ID'))}</code></p>")
    out.append(f"<p>스코프: <code>{_e(' '.join(INVITE_SCOPES))}</code> · 권한 값: <code>{INVITE_PERMISSIONS}</code></p>")
    out.append("<table><tr><th>필요 권한</th><th>비트값</th></tr>")
    for name, bit in REQUIRED_PERMISSIONS:
        out.append(f"<tr><td>{_e(name)}</td><td><code>{bit}</code></td></tr>")
    out.append("</table><p class=\"muted\">슬래시 명령의 직접 응답은 인터랙션 응답으로 가지만, 예약 알림·일정 알림처럼 "
               "봇이 채널에 직접 보내는 메시지에는 위 채널 권한이 필요합니다.</p>")
    out.append("<h3>인텐트</h3><table><tr><th>인텐트</th><th>설명</th></tr>")
    for name, note in REQUIRED_INTENTS:
        out.append(f"<tr><td>{_e(name)}</td><td>{_e(note)}</td></tr>")
    out.append("</table></section></main></body></html>")
    return "\n".join(out)


def register_web(app, bot) -> None:
    """Flask keep-alive 앱에 /help, /commands 경로를 추가한다 (`/` 는 건드리지 않음)."""

    def help_page():
        return render_help_html(bot), 200, {"Content-Type": "text/html; charset=utf-8"}

    app.add_url_rule("/help", "help_page", help_page)
    app.add_url_rule("/commands", "commands_page", help_page)
