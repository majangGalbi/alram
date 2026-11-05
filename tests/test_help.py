"""help.py (/도움말 + 웹 /help) 테스트."""
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot as botmod  # noqa: E402
import help as helpmod  # noqa: E402


def all_registered_names():
    names = []
    for top in botmod.bot.tree.get_commands():
        if isinstance(top, app_commands.Group):
            names += [c.qualified_name for c in top.walk_commands() if not isinstance(c, app_commands.Group)]
        else:
            names.append(top.name)
    return names


def make_bot():
    return commands.Bot(command_prefix="!", intents=discord.Intents.default())


def check_embed_limits(embed):
    assert len(embed.fields) <= 25
    for f in embed.fields:
        assert len(f.name) <= 256
        assert len(f.value) <= 1024
    assert len(embed.description or "") <= 4096
    assert len(embed) <= 6000


# --- 자동 생성 -------------------------------------------------------------
def test_help_registered_on_real_bot():
    assert "도움말" in all_registered_names()


def test_all_registered_commands_in_help():
    infos = helpmod.collect_commands(botmod.bot)
    names = {i.name for i in infos}
    registered = all_registered_names()
    assert len(registered) >= 18
    assert set(registered) == names
    grouped = helpmod.group_by_category(infos)
    in_embeds = set()
    for cat, cmds in grouped.items():
        embed = helpmod.build_category_embed(cat, cmds)
        text = " ".join(f.name + f.value for f in embed.fields)
        for c in cmds:
            if f"/{c.name}" in text:
                in_embeds.add(c.name)
    assert in_embeds == names


def test_known_commands_are_not_other():
    infos = helpmod.collect_commands(botmod.bot)
    for i in infos:
        if i.name in helpmod.COMMAND_CATEGORIES:
            assert i.category != helpmod.OTHER_CATEGORY, i.name
    # 매핑 테이블이 실제 존재하는 카테고리만 가리키는지
    assert set(helpmod.COMMAND_CATEGORIES.values()) <= set(helpmod.CATEGORIES)


def test_params_read_from_tree():
    infos = {i.name: i for i in helpmod.collect_commands(botmod.bot)}
    yeyak = infos["예약"]
    assert [p.name for p in yeyak.params] == ["일시", "멘션", "메시지"]
    assert all(p.required for p in yeyak.params)
    assert yeyak.usage == "/예약 <일시> <멘션> <메시지>"
    assert "[표현식]" in infos["주사위"].usage


def test_unmapped_command_goes_to_other():
    b = make_bot()

    @app_commands.command(name="레벨", description="레벨을 보여줍니다.")
    @app_commands.describe(사용자="조회할 사용자")
    async def level(interaction: discord.Interaction, 사용자: str = None):
        pass

    @app_commands.command(name="예약", description="x")
    async def yeyak(interaction: discord.Interaction):
        pass

    b.tree.add_command(level)
    b.tree.add_command(yeyak)
    infos = {i.name: i for i in helpmod.collect_commands(b)}
    assert infos["레벨"].category == helpmod.OTHER_CATEGORY
    assert infos["레벨"].params[0].name == "사용자" and not infos["레벨"].params[0].required
    assert infos["예약"].category == "예약 알림"
    grouped = helpmod.group_by_category(list(infos.values()))
    assert list(grouped)[-1] == helpmod.OTHER_CATEGORY
    assert "/레벨" in helpmod.render_help_html(b)


def test_group_subcommands_expanded():
    b = make_bot()
    grp = app_commands.Group(name="레벨", description="레벨 관련")

    @grp.command(name="보기", description="레벨 보기")
    async def view(interaction: discord.Interaction):
        pass

    b.tree.add_command(grp)
    infos = helpmod.collect_commands(b)
    assert [i.name for i in infos] == ["레벨 보기"]
    assert infos[0].category == helpmod.OTHER_CATEGORY


# --- 임베드 제한 -----------------------------------------------------------
def test_real_embeds_within_limits():
    grouped = helpmod.group_by_category(helpmod.collect_commands(botmod.bot))
    check_embed_limits(helpmod.build_overview_embed(grouped))
    for cat, cmds in grouped.items():
        check_embed_limits(helpmod.build_category_embed(cat, cmds))


def test_huge_category_respects_limits():
    long = "가" * 900
    cmds = [helpmod.CommandInfo(f"명령{i}", long,
                                [helpmod.ParamInfo(f"p{j}", long, j % 2 == 0) for j in range(5)],
                                helpmod.OTHER_CATEGORY, ["/x " + long]) for i in range(60)]
    embed = helpmod.build_category_embed(helpmod.OTHER_CATEGORY, cmds)
    check_embed_limits(embed)
    assert embed.fields[-1].name == "➕ 더 있는 명령"
    grouped = {helpmod.OTHER_CATEGORY: cmds}
    check_embed_limits(helpmod.build_overview_embed(grouped))
    many = {f"c{i}": cmds[:1] for i in range(40)}
    check_embed_limits(helpmod.build_overview_embed(many))


# --- 슬래시 명령 동작 --------------------------------------------------------
def make_interaction():
    it = MagicMock()
    it.response.send_message = AsyncMock()
    it.response.edit_message = AsyncMock()
    return it


@pytest.mark.asyncio
async def test_handle_help_is_ephemeral_with_select():
    it = make_interaction()
    await helpmod.handle_help(it, botmod.bot, None)
    kwargs = it.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "📖 도움말"
    view = kwargs["view"]
    select = view.children[0]
    assert isinstance(select, discord.ui.Select)
    labels = [o.label for o in select.options]
    assert "투표" in labels and "달력 일정표" in labels
    assert len(select.options) <= 25


@pytest.mark.asyncio
async def test_handle_help_category_and_select_switch():
    it = make_interaction()
    await helpmod.handle_help(it, botmod.bot, "투표")
    embed = it.response.send_message.call_args.kwargs["embed"]
    assert "투표" in embed.title
    assert {f.name for f in embed.fields} == {"/투표", "/투표마감"}

    view = it.response.send_message.call_args.kwargs["view"]
    select = view.children[0]
    select._values = ["뽑기 도구"]  # 사용자가 드롭다운에서 고른 것처럼
    it2 = make_interaction()
    await select.callback(it2)
    embed2 = it2.response.edit_message.call_args.kwargs["embed"]
    assert {f.name for f in embed2.fields} == {"/주사위", "/뽑기", "/팀나누기"}


def test_resolve_category():
    grouped = helpmod.group_by_category(helpmod.collect_commands(botmod.bot))
    assert helpmod.resolve_category("투표", grouped) == "투표"
    assert helpmod.resolve_category("/주사위", grouped) == "뽑기 도구"
    assert helpmod.resolve_category("달력", grouped) == "달력 일정표"
    assert helpmod.resolve_category("없는거", grouped) is None
    assert helpmod.resolve_category(None, grouped) is None


# --- 웹 페이지 ---------------------------------------------------------------
def test_flask_help_page():
    client = botmod.app.test_client()
    assert client.get("/").status_code == 200
    for path in ("/help", "/commands"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")
        page = resp.get_data(as_text=True)
        for name in all_registered_names():
            assert f"/{name}" in page
        assert "prefers-color-scheme:dark" in page
        assert 'name="viewport"' in page
        assert "permissions=117760" in page and "applications.commands" in page


def test_html_escapes_and_has_no_server_data():
    b = make_bot()

    @app_commands.command(name="xss", description="<script>alert(1)</script>")
    async def x(interaction: discord.Interaction):
        pass

    b.tree.add_command(x)
    page = helpmod.render_help_html(b)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_invite_permissions_value():
    p = discord.Permissions(view_channel=True, send_messages=True, embed_links=True,
                            attach_files=True, read_message_history=True)
    assert helpmod.INVITE_PERMISSIONS == p.value == 117760


def test_invite_url():
    url = helpmod.invite_url(123)
    assert url == "https://discord.com/oauth2/authorize?client_id=123&permissions=117760&scope=bot+applications.commands"


def test_simple_namespace_tree_tolerated():
    fake = SimpleNamespace(tree=SimpleNamespace(get_commands=lambda: []))
    assert helpmod.collect_commands(fake) == []
    assert "봇 명령 도움말" in helpmod.render_help_html(fake)
