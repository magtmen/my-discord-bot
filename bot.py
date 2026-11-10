import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ.get("DISCORD_TOKEN")
CHECK_INTERVAL_MIN = 10
STATE_FILE = Path("state.json")
STEAM_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"

# Подключаем переводчик
try:
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source='auto', target='ru')
except Exception as e:
    print(f"Не удалось инициализировать переводчик: {e}")
    translator = None

GAMES = {
    "dota": {
        "name": "Dota 2",
        "app_id": 570,
        "color": 0xB22222,
        "keywords": ["update", "patch", "обновлен", "7."],
    },
    "cs": {
        "name": "Counter-Strike 2",
        "app_id": 730,
        "color": 0xF5A623,
        "keywords": ["update", "release notes", "обновлен"],
    },
}

GAME_CHOICES = [
    app_commands.Choice(name=g["name"], value=key) for key, g in GAMES.items()
]


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"last_gid": {}, "guilds": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_last_gid(self, game: str) -> str | None:
        return self.data["last_gid"].get(game)

    def set_last_gid(self, game: str, gid: str) -> None:
        self.data["last_gid"][game] = gid
        self.save()

    def set_target(self, guild_id: int, game: str, channel_id: int, role_id: int):
        guild = self.data["guilds"].setdefault(str(guild_id), {})
        guild[game] = {"channel_id": channel_id, "role_id": role_id}
        self.save()

    def remove_target(self, guild_id: int, game: str) -> bool:
        guild = self.data["guilds"].get(str(guild_id), {})
        if game in guild:
            del guild[game]
            self.save()
            return True
        return False

    def targets(self, game: str):
        for guild_id, games in self.data["guilds"].items():
            if game in games:
                yield int(guild_id), games[game]["channel_id"], games[game]["role_id"]

    def guild_settings(self, guild_id: int) -> dict:
        return self.data["guilds"].get(str(guild_id), {})


def tr(text: str) -> str:
    """Функция перевода текста"""
    if not translator or not text.strip():
        return text
    try:
        res = translator.translate(text)
        return res if res else text
    except Exception as e:
        print(f"Ошибка перевода: {e}")
        return text


def clean(text: str, limit: int = 500) -> str:
    text = re.sub(r"\[/?[^\]]+\]", "", text)
    text = re.sub(r"\{STEAM_CLAN_IMAGE\}\S*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    cleaned = text[:limit] + ("…" if len(text) > limit else "")
    return tr(cleaned)


def is_update(game: str, item: dict) -> bool:
    keywords = GAMES[game]["keywords"]
    if not keywords:
        return True
    title = item["title"].lower()
    return any(k.lower() in title for k in keywords)


def build_embed(game: str, item: dict) -> discord.Embed:
    info = GAMES[game]
    title_ru = tr(item["title"])
    
    embed = discord.Embed(
        title=title_ru[:256],
        url=item["url"],
        description=clean(item.get("contents", "")),
        color=info["color"],
    )
    embed.set_footer(text=f"{info['name']} • Steam News (RU)")
    embed.timestamp = datetime.fromtimestamp(item["date"], tz=timezone.utc)
    return embed


class UpdateBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.state = State(STATE_FILE)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        self.check_updates.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def fetch_news(self, app_id: int) -> list[dict]:
        params = {
            "appid": app_id,
            "count": 10,
            "maxlength": 700,
            "feeds": "steam_community_announcements",
            "format": "json",
        }
        async with self.session.get(
            STEAM_API, params=params, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["appnews"]["newsitems"]

    async def send_update(self, game, item, channel_id, role_id) -> bool:
        channel = self.get_channel(channel_id)
        if channel is None:
            return False
        try:
            await channel.send(
                content=f"<@&{role_id}>",
                embed=build_embed(game, item),
                allowed_mentions=discord.AllowedMentions(
                    roles=[discord.Object(id=role_id)]
                ),
            )
            return True
        except discord.HTTPException as e:
            print(f"[{game}] не удалось отправить в {channel_id}: {e}")
            return False

    @tasks.loop(minutes=CHECK_INTERVAL_MIN)
    async def check_updates(self):
        for game in GAMES:
            try:
                items = await self.fetch_news(GAMES[game]["app_id"])
            except Exception as e:
                print(f"[{game}] ошибка запроса к Steam: {e}")
                continue
            if not items:
                continue

            last_gid = self.state.get_last_gid(game)
            if last_gid is None:
                self.state.set_last_gid(game, items[0]["gid"])
                print(f"[{game}] первый запуск, старые посты пропущены")
                continue

            new_items = []
            for item in items:
                if item["gid"] == last_gid:
                    break
                new_items.append(item)

            for item in reversed(new_items):
                if not is_update(game, item):
                    continue
                for _, channel_id, role_id in list(self.state.targets(game)):
                    await self.send_update(game, item, channel_id, role_id)
                    await asyncio.sleep(1)
                print(f"[{game}] отправлено: {item['title']}")

            if new_items:
                self.state.set_last_gid(game, new_items[0]["gid"])

    @check_updates.before_loop
    async def before_check(self):
        await self.wait_until_ready()


bot = UpdateBot()


@bot.tree.command(name="setup", description="Включить уведомления об обновлениях игры")
@app_commands.describe(
    game="Игра",
    channel="Канал, куда слать уведомления",
    role="Роль, которую нужно пинговать",
)
@app_commands.choices(game=GAME_CHOICES)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def setup_cmd(
    interaction: discord.Interaction,
    game: app_commands.Choice[str],
    channel: discord.TextChannel,
    role: discord.Role,
):
    me = interaction.guild.me
    perms = channel.permissions_for(me)
    missing = [
        name
        for name, ok in [
            ("Просмотр канала", perms.view_channel),
            ("Отправка сообщений", perms.send_messages),
            ("Встраивание ссылок", perms.embed_links),
        ]
        if not ok
    ]
    if missing:
        await interaction.response.send_message(
            f"У бота нет прав в {channel.mention}: {', '.join(missing)}.",
            ephemeral=True,
        )
        return

    bot.state.set_target(interaction.guild_id, game.value, channel.id, role.id)
    text = f"Готово. Обновления **{game.name}** будут приходить в {channel.mention} с пингом {role.mention}."
    await interaction.response.send_message(
        text, allowed_mentions=discord.AllowedMentions.none()
    )


@bot.tree.command(name="disable", description="Выключить уведомления об обновлениях игры")
@app_commands.choices(game=GAME_CHOICES)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def disable_cmd(interaction: discord.Interaction, game: app_commands.Choice[str]):
    removed = bot.state.remove_target(interaction.guild_id, game.value)
    msg = f"Уведомления для **{game.name}** выключены." if removed else f"Для **{game.name}** уведомления и не были включены."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="status", description="Показать текущие настройки")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction):
    settings = bot.state.guild_settings(interaction.guild_id)
    lines = []
    for key, info in GAMES.items():
        if key in settings:
            s = settings[key]
            lines.append(f"**{info['name']}**: <#{s['channel_id']}> → <@&{s['role_id']}>")
        else:
            lines.append(f"**{info['name']}**: выключено")
    await interaction.response.send_message(
        "\n".join(lines),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="test", description="Отправить последнюю новость игры (тест)")
@app_commands.choices(game=GAME_CHOICES)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def test_cmd(interaction: discord.Interaction, game: app_commands.Choice[str]):
    target = bot.state.guild_settings(interaction.guild_id).get(game.value)
    if not target:
        await interaction.response.send_message(f"Сначала настрой **{game.name}** через /setup.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        items = await bot.fetch_news(GAMES[game.value]["app_id"])
    except Exception as e:
        await interaction.followup.send(f"Не удалось получить новости: {e}")
        return
    if not items:
        await interaction.followup.send("Новостей не найдено.")
        return

    ok = await bot.send_update(
        game.value, items[0], target["channel_id"], target["role_id"]
    )
    await interaction.followup.send(
        "Отправлено (проверьте канал)." if ok else "Не получилось отправить, проверь права бота."
    )


@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Задай переменную окружения DISCORD_TOKEN")
    bot.run(TOKEN)
