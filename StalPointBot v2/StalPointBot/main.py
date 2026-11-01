from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from bot.database import Database, json_dump, utc_now


# ==================== ФАЙЛЫ И НАСТРОЙКИ ====================

ROOT = Path(__file__).resolve().parent

load_dotenv(ROOT / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError(
        "В файле .env отсутствует DISCORD_TOKEN."
    )

with (ROOT / "config.json").open(encoding="utf-8") as file:
    BASE_CONFIG = json.load(file)

logging.basicConfig(
    level=getattr(
        logging,
        BASE_CONFIG.get("log_level", "INFO").upper(),
        logging.INFO,
    ),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("stalpoint")

database = Database(ROOT / BASE_CONFIG["database_path"])


# ==================== ПОЛЯ АДМИНИСТРАТИВНОЙ ПАНЕЛИ ====================

# Формат:
# (название поля, путь в конфигурации, тип выбора)

GROUPS = {
    "channels": {
        "label": "Публичные и служебные каналы",
        "fields": [
            ("Продать ресурсы", "channels.resources", "text"),
            ("Ломбард", "channels.pawn", "text"),
            ("Купить бусты", "channels.boosts", "text"),
            ("Репутация арсенала", "channels.arsenal", "text"),
            ("Связаться с нами", "channels.contact", "text"),
            ("Инструменты сотрудников", "channels.staff_panel", "text"),
            ("История тикетов", "channels.transcripts", "text"),
            ("Журнал действий", "channels.audit", "text"),
        ],
    },

    "categories": {
        "label": "Категории тикетов",
        "fields": [
            ("Открытые: ресурсы", "categories.open.resources", "category"),
            ("Открытые: ломбард", "categories.open.pawn", "category"),
            ("Открытые: бусты", "categories.open.boosts", "category"),
            ("Открытые: арсенал", "categories.open.arsenal", "category"),
            ("Открытые: обращения", "categories.open.contact", "category"),
            ("Закрытые тикеты", "categories.closed", "category"),
        ],
    },

    "staff": {
        "label": "Роли доступа сотрудников",
        "fields": [
            ("Ресурсы", "roles.staff.resources", "roles"),
            ("Ломбард", "roles.staff.pawn", "roles"),
            ("Бусты", "roles.staff.boosts", "roles"),
            ("Арсенал", "roles.staff.arsenal", "roles"),
            ("Обращения", "roles.staff.contact", "roles"),
        ],
    },

    "notifications": {
        "label": "Роли уведомлений",
        "fields": [
            ("Ресурсы", "roles.notifications.resources", "roles"),
            ("Ломбард", "roles.notifications.pawn", "roles"),
            ("Бусты", "roles.notifications.boosts", "roles"),
            ("Арсенал", "roles.notifications.arsenal", "roles"),
            ("Поддержка", "roles.notifications.support", "roles"),
            ("Сотрудничество", "roles.notifications.cooperation", "roles"),
            ("Идеи", "roles.notifications.ideas", "roles"),
            ("Другое", "roles.notifications.other", "roles"),
        ],
    },

    "permissions": {
        "label": "Права редактирования и управления",
        "fields": [
            (
                "Редакторы закупки ресурсов",
                "roles.price_editors.resources",
                "roles",
            ),
            (
                "Редакторы цен бустов",
                "roles.price_editors.boosts",
                "roles",
            ),
            (
                "Редакторы цен арсенала",
                "roles.price_editors.arsenal",
                "roles",
            ),
            (
                "Управление ограничениями",
                "roles.restriction_managers",
                "roles",
            ),
            (
                "Редакторы FAQ",
                "roles.faq_editors",
                "roles",
            ),
            (
                "Импорт прайсов",
                "roles.catalog_importers",
                "roles",
            ),
        ],
    },
}


def get_path(document, path):
    value = document

    for part in path.split("."):
        value = value[part]

    return value


def set_path(document, path, value):
    parts = path.split(".")
    target = document

    for part in parts[:-1]:
        target = target.setdefault(part, {})

    target[parts[-1]] = value


def merge_config(base, overrides):
    result = copy.deepcopy(base)

    for key, value in overrides.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)

    return result


def read_server_config(guild_id):
    overrides = database.setting_get(
        f"guild_config:{guild_id}",
        {},
    )
    return merge_config(BASE_CONFIG, overrides)


def save_server_field(guild_id, path, value, actor_id):
    """
    Считывание и изменение настроек выполняются одной транзакцией.
    Это не позволяет параллельным настройкам затирать друг друга.
    """
    key = f"guild_config:{guild_id}"

    with database.transaction() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (key,),
        ).fetchone()

        overrides = json.loads(row["value_json"]) if row else {}
        set_path(overrides, path, value)
        overrides["guild_id"] = guild_id

        now = utc_now()

        connection.execute(
            """
            INSERT INTO settings(key, value_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (key, json_dump(overrides), now),
        )

        connection.execute(
            """
            INSERT INTO audit_events(
                kind, actor_id, payload_json, created_at
            )
            VALUES(?, ?, ?, ?)
            """,
            (
                "configuration_changed",
                actor_id,
                json_dump(
                    {
                        "guild_id": guild_id,
                        "field": path,
                        "value": value,
                    }
                ),
                now,
            ),
        )


def bind_server(guild_id):
    """
    Проект рассчитан на один сервер.
    Если guild_id=0, привязываем его при первом открытии /admin.
    """
    configured = int(BASE_CONFIG.get("guild_id", 0))

    if configured and configured != guild_id:
        return False

    with database.transaction() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings "
            "WHERE key='configured_guild_id'"
        ).fetchone()

        if row:
            return int(json.loads(row["value_json"])) == guild_id

        connection.execute(
            """
            INSERT INTO settings(key, value_json, updated_at)
            VALUES('configured_guild_id', ?, ?)
            """,
            (json_dump(guild_id), utc_now()),
        )

    return True


# ==================== ИНТЕРФЕЙС НАСТРОЕК ====================

class GroupSelect(discord.ui.Select):
    def __init__(self, selected):
        super().__init__(
            placeholder="1. Выберите раздел настройки",
            row=0,
            options=[
                discord.SelectOption(
                    label=group["label"],
                    value=key,
                    default=key == selected,
                )
                for key, group in GROUPS.items()
            ],
        )

    async def callback(self, interaction):
        view = self.view
        view.group = self.values[0]
        view.field_index = 0

        await interaction.response.defer()
        await view.refresh(interaction)


class FieldSelect(discord.ui.Select):
    def __init__(self, group, selected_index):
        fields = GROUPS[group]["fields"]

        super().__init__(
            placeholder="2. Выберите, что настроить",
            row=1,
            options=[
                discord.SelectOption(
                    label=label,
                    value=str(index),
                    default=index == selected_index,
                )
                for index, (label, _, _) in enumerate(fields)
            ],
        )

    async def callback(self, interaction):
        view = self.view
        view.field_index = int(self.values[0])

        await interaction.response.defer()
        await view.refresh(interaction)


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, kind):
        channel_type = (
            discord.ChannelType.category
            if kind == "category"
            else discord.ChannelType.text
        )

        super().__init__(
            placeholder=(
                "3. Выберите категорию — сохранится сразу"
                if kind == "category"
                else "3. Выберите канал — сохранится сразу"
            ),
            channel_types=[channel_type],
            min_values=1,
            max_values=1,
            row=2,
        )

    async def callback(self, interaction):
        await interaction.response.defer()

        view = self.view
        _, path, _ = view.current_field()

        await asyncio.to_thread(
            save_server_field,
            view.guild_id,
            path,
            self.values[0].id,
            interaction.user.id,
        )

        await view.refresh(interaction, saved=True)


class RolesPicker(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="3. Выберите роли — сохранятся сразу",
            min_values=0,
            max_values=10,
            row=2,
        )

    async def callback(self, interaction):
        await interaction.response.defer()

        if any(role.is_default() or role.managed for role in self.values):
            await interaction.followup.send(
                "Выберите обычные роли сотрудников: "
                "не @everyone и не служебные роли ботов.",
                ephemeral=True,
            )
            return

        view = self.view
        _, path, _ = view.current_field()

        await asyncio.to_thread(
            save_server_field,
            view.guild_id,
            path,
            [role.id for role in self.values],
            interaction.user.id,
        )

        await view.refresh(interaction, saved=True)


class AdminView(discord.ui.View):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=900)

        self.owner_id = owner_id
        self.guild_id = guild_id
        self.group = "channels"
        self.field_index = 0

        self.rebuild()

    def current_field(self):
        return GROUPS[self.group]["fields"][self.field_index]

    def rebuild(self):
        self.clear_items()

        self.add_item(GroupSelect(self.group))
        self.add_item(FieldSelect(self.group, self.field_index))

        _, _, kind = self.current_field()

        if kind == "roles":
            self.add_item(RolesPicker())
        else:
            self.add_item(ChannelPicker(kind))

        refresh_button = discord.ui.Button(
            label="Обновить",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        refresh_button.callback = self.refresh_button
        self.add_item(refresh_button)

    async def interaction_check(self, interaction):
        permitted = (
            interaction.guild_id == self.guild_id
            and interaction.user.id == self.owner_id
            and interaction.permissions.administrator
        )

        if not permitted:
            await interaction.response.send_message(
                "Эта панель доступна только открывшему её "
                "администратору. Откройте собственную через /admin.",
                ephemeral=True,
            )

        return permitted

    async def make_embed(self, saved=False):
        config = await asyncio.to_thread(
            read_server_config,
            self.guild_id,
        )

        label, path, kind = self.current_field()
        current = get_path(config, path)

        if kind == "roles":
            value = (
                "\n".join(f"<@&{role_id}>" for role_id in current)
                if current
                else "Не назначены."
            )
        else:
            value = f"<#{current}>" if current else "Не задан."

        lines = []

        for field_label, field_path, field_kind in GROUPS[self.group]["fields"]:
            field_value = get_path(config, field_path)
            marker = "✅" if field_value else "▫️"

            if field_kind == "roles":
                detail = (
                    f"ролей: {len(field_value)}"
                    if field_value
                    else "не назначены"
                )
            else:
                detail = (
                    f"<#{field_value}>"
                    if field_value
                    else "не задан"
                )

            lines.append(f"{marker} **{field_label}:** {detail}")

        embed = discord.Embed(
            title="⚙️ Настройка StalPoint",
            description=(
                "Выберите раздел, затем поле и нужный канал, "
                "категорию или роли.\n\n"
                "**Каждый выбор сохраняется сразу в SQLite.**\n"
                "Настройки не пропадут после перезапуска."
            ),
            color=discord.Color.green() if saved else discord.Color.blue(),
        )

        embed.add_field(
            name=GROUPS[self.group]["label"],
            value="\n".join(lines),
            inline=False,
        )

        embed.add_field(
            name=f"Выбрано: {label}",
            value=f"Текущее значение:\n{value}",
            inline=False,
        )

        if kind == "roles":
            embed.add_field(
                name="Назначение ролей",
                value=(
                    "Можно выбрать несколько ролей. "
                    "Новый выбор заменяет предыдущий список.\n"
                    "Роли уведомлений не получают автоматически "
                    "право редактировать цены."
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                "Сохранено. Панель действует 15 минут; "
                "повторное открытие: /admin"
                if saved
                else
                "Это настройка проекта. Публикация панелей "
                "будет добавлена следующим комплектом."
            )
        )

        return embed

    async def refresh(self, interaction, saved=False):
        self.rebuild()

        await interaction.edit_original_response(
            embed=await self.make_embed(saved=saved),
            view=self,
        )

    async def refresh_button(self, interaction):
        await interaction.response.defer()
        await self.refresh(interaction)

    async def on_error(self, interaction, error, item):
        logger.error(
            "Ошибка административной панели",
            exc_info=(type(error), error, error.__traceback__),
        )

        text = (
            "Не удалось выполнить действие. "
            "Подробности находятся в терминале бота."
        )

        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(
                text,
                ephemeral=True,
            )


# ==================== DISCORD-КЛИЕНТ ====================

class StalPointClient(discord.Client):
    def __init__(self):
        # На этапе настройки чтение сообщений пока не используется.
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await asyncio.to_thread(database.migrate)

        await asyncio.to_thread(
            database.initial_import,
            ROOT / "data",
            BASE_CONFIG["limits"]["max_price"],
            BASE_CONFIG["arsenal"]["initial_seller_status"],
        )

        configured_guild = int(BASE_CONFIG.get("guild_id", 0))

        if not configured_guild:
            configured_guild = int(
                await asyncio.to_thread(
                    database.setting_get,
                    "configured_guild_id",
                    0,
                )
            )

        if configured_guild:
            guild_object = discord.Object(id=configured_guild)
            self.tree.copy_global_to(guild=guild_object)
            commands = await self.tree.sync(guild=guild_object)

            logger.info(
                "Команды зарегистрированы на сервере %s: %s",
                configured_guild,
                len(commands),
            )
        else:
            # Получаем серверы приложения через HTTP.
            # Это позволяет зарегистрировать /admin на сервере,
            # даже когда guild_id пока оставлен равным 0.
            async for guild in self.fetch_guilds(limit=None):
                guild_object = discord.Object(id=guild.id)
                self.tree.copy_global_to(guild=guild_object)
                commands = await self.tree.sync(guild=guild_object)

                logger.info(
                    "Команды зарегистрированы на сервере %s: %s",
                    guild.id,
                    len(commands),
                )

    async def on_ready(self):
        logger.info("Бот подключён: %s", self.user)
        logger.info("Откройте /admin на сервере Discord.")
        logger.info(
            "Текущий этап: настройка каналов, категорий и ролей. "
            "Торговые тикеты ещё не подключены."
        )


client = StalPointClient()


@client.tree.command(
    name="admin",
    description="Настройка каналов, категорий и ролей StalPoint",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def admin(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    allowed = await asyncio.to_thread(
        bind_server,
        interaction.guild_id,
    )

    if not allowed:
        await interaction.followup.send(
            "Этот экземпляр бота настроен для другого сервера.",
            ephemeral=True,
        )
        return

    view = AdminView(
        owner_id=interaction.user.id,
        guild_id=interaction.guild_id,
    )

    await interaction.followup.send(
        embed=await view.make_embed(),
        view=view,
        ephemeral=True,
    )


@client.tree.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = "Эта команда доступна только администраторам."
    else:
        logger.error(
            "Ошибка команды",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = (
            "Не удалось открыть панель. "
            "Посмотрите ошибку в терминале бота."
        )

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)