from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

from bot.database import json_dump, utc_now


logger = logging.getLogger("stalpoint.tickets")

DIRECTIONS = {
    "resources": "Продать ресурсы",
    "pawn": "Ломбард",
    "boosts": "Купить бусты",
    "arsenal": "Купить репутацию арсенала",
    "contact": "Связаться с нами",
}

TOPICS = {
    "support": (
        "🛟",
        "Поддержка",
        "Проблема со сделкой, оплатой, почтой",
    ),
    "cooperation": (
        "🤝",
        "Сотрудничество",
        "Реклама, блогерам, партнёрство",
    ),
    "ideas": (
        "💡",
        "Идеи",
        "Предложения по магазину и боту",
    ),
    "other": (
        "📌",
        "Другое",
        "Другие вопросы и предложения",
    ),
}


def split_text(text, limit=1800):
    """Разбиение без потери текста."""
    while text:
        if len(text) <= limit:
            yield text
            return

        cut = text.rfind("\n", 0, limit)

        if cut < limit // 2:
            cut = limit

        yield text[:cut]
        text = text[cut:]


async def reply(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(
            text,
            ephemeral=True,
        )


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        logger.error(
            "Ошибка компонента",
            exc_info=(type(error), error, error.__traceback__),
        )

        await reply(
            interaction,
            "Не удалось выполнить действие. "
            "Подробности находятся в терминале бота.",
        )


class PublicPanel(SafeView):
    def __init__(self, service, direction):
        super().__init__(timeout=None)

        self.service = service
        self.direction = direction

        if direction == "contact":
            menu = discord.ui.Select(
                placeholder="Выбери тему…",
                custom_id="stalpoint:public:contact:v1",
                options=[
                    discord.SelectOption(
                        label=label,
                        value=topic,
                        emoji=emoji,
                        description=description,
                    )
                    for topic, (
                        emoji,
                        label,
                        description,
                    ) in TOPICS.items()
                ],
            )

            menu.callback = self.choose_topic
            self.add_item(menu)

        else:
            button = discord.ui.Button(
                label="Создать тикет",
                style=discord.ButtonStyle.success,
                custom_id=f"stalpoint:public:{direction}:v1",
            )

            button.callback = self.create
            self.add_item(button)

    async def create(self, interaction):
        await self.service.create_ticket(
            interaction,
            self.direction,
            None,
        )

    async def choose_topic(self, interaction):
        topic = interaction.data["values"][0]

        if topic not in TOPICS:
            await reply(interaction, "Неизвестная тема обращения.")
            return

        await self.service.create_ticket(
            interaction,
            "contact",
            topic,
        )


class TicketControls(SafeView):
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(
        label="Нужна помощь сотрудника",
        style=discord.ButtonStyle.primary,
        custom_id="stalpoint:ticket:help:v1",
    )
    async def help_button(self, interaction, button):
        ticket = await self.service.ticket_for(interaction.channel_id)

        if not ticket or ticket["state"] != "open":
            await reply(interaction, "Это обращение уже недоступно.")
            return

        if interaction.user.id != ticket["owner_id"]:
            await reply(
                interaction,
                "Эта кнопка предназначена владельцу обращения.",
            )
            return

        if ticket["direction"] != "contact":
            await reply(
                interaction,
                "Напишите вопрос прямо в тикете. "
                "Торговый интерфейс будет подключён следующим комплектом.",
            )
            return

        notification = await self.service.query_one(
            """
            SELECT state FROM notifications
            WHERE ticket_id=? AND notification_key='first_message'
            """,
            (ticket["id"],),
        )

        if notification and notification["state"] == "sent":
            await reply(
                interaction,
                "Сотрудник уже уведомлён. "
                "Дополнительные сведения можно написать здесь.",
            )
            return

        await reply(
            interaction,
            self.service.templates["contact_help_prompt"],
        )

    @discord.ui.button(
        label="Всё понятно — закрыть",
        style=discord.ButtonStyle.secondary,
        custom_id="stalpoint:ticket:faq_close:v1",
    )
    async def faq_close(self, interaction, button):
        ticket = await self.service.ticket_for(interaction.channel_id)

        if not ticket or ticket["direction"] != "contact":
            await reply(
                interaction,
                "Эта кнопка доступна только в обращениях "
                "«Связаться с нами».",
            )
            return

        if interaction.user.id != ticket["owner_id"]:
            await reply(
                interaction,
                "Закрыть обращение этой кнопкой может только его владелец.",
            )
            return

        await interaction.response.send_modal(
            CloseModal(
                self.service,
                ticket["id"],
                faq=True,
            )
        )

    @discord.ui.button(
        label="Принять в работу",
        style=discord.ButtonStyle.success,
        custom_id="stalpoint:ticket:claim:v1",
    )
    async def claim(self, interaction, button):
        await interaction.response.defer(ephemeral=True)

        ticket = await self.service.ticket_for(interaction.channel_id)

        if not ticket or not await self.service.is_staff(
            interaction.user,
            ticket,
        ):
            await reply(interaction, "Недостаточно прав.")
            return

        async with self.service.lock(ticket["id"]):
            changed = await self.service.execute(
                """
                UPDATE tickets
                SET assigned_to=?, staff_status='in_progress',
                    updated_at=?
                WHERE id=? AND state='open'
                  AND (assigned_to IS NULL OR assigned_to=?)
                """,
                (
                    interaction.user.id,
                    utc_now(),
                    ticket["id"],
                    interaction.user.id,
                ),
            )

        await reply(
            interaction,
            "Тикет принят в работу."
            if changed
            else "Тикет уже закрывается или принят другим сотрудником.",
        )

    @discord.ui.button(
        label="Закрыть — сотрудник",
        style=discord.ButtonStyle.danger,
        custom_id="stalpoint:ticket:staff_close:v1",
    )
    async def staff_close(self, interaction, button):
        ticket = await self.service.ticket_for(interaction.channel_id)

        if not ticket or not await self.service.is_staff(
            interaction.user,
            ticket,
        ):
            await reply(interaction, "Недостаточно прав.")
            return

        await interaction.response.send_modal(
            CloseModal(
                self.service,
                ticket["id"],
                faq=False,
            )
        )


class CloseModal(discord.ui.Modal):
    def __init__(self, service, ticket_id, faq):
        super().__init__(title="Подтверждение закрытия")

        self.service = service
        self.ticket_id = ticket_id
        self.faq = faq

        self.reason = discord.ui.TextInput(
            label="Причина закрытия",
            default="Вопрос решён" if faq else None,
            placeholder="Кратко укажите причину",
            max_length=400,
            required=True,
        )

        self.confirmation = discord.ui.TextInput(
            label="Для подтверждения введите ЗАКРЫТЬ",
            max_length=7,
            min_length=7,
            required=True,
        )

        self.add_item(self.reason)
        self.add_item(self.confirmation)

    async def on_submit(self, interaction):
        if self.confirmation.value.strip().upper() != "ЗАКРЫТЬ":
            await reply(
                interaction,
                "Подтверждение не совпало. Тикет не закрыт.",
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        ticket = await self.service.query_one(
            "SELECT * FROM tickets WHERE id=?",
            (self.ticket_id,),
        )

        if not ticket or interaction.channel_id != ticket["channel_id"]:
            await reply(interaction, "Тикет не найден.")
            return

        if self.faq:
            allowed = (
                ticket["direction"] == "contact"
                and interaction.user.id == ticket["owner_id"]
            )
        else:
            allowed = await self.service.is_staff(
                interaction.user,
                ticket,
            )

        if not allowed:
            await reply(interaction, "Недостаточно прав.")
            return

        try:
            await self.service.close_ticket(
                ticket,
                interaction.user.id,
                self.reason.value.strip(),
            )
        except Exception:
            logger.exception(
                "Не завершено закрытие тикета %s",
                self.ticket_id,
            )

            await reply(
                interaction,
                "Закрытие не завершено. Сохранённый этап не потерян. "
                "Бот повторит попытку автоматически; сотрудник также "
                "может повторно нажать кнопку закрытия.",
            )
            return

        # При удалении канала исходное взаимодействие может
        # стать недоступно. Само закрытие уже выполнено.
        try:
            await reply(interaction, "Тикет закрыт, история сохранена.")
        except discord.HTTPException:
            pass

    async def on_error(self, interaction, error):
        logger.error(
            "Ошибка формы закрытия",
            exc_info=(type(error), error, error.__traceback__),
        )
        await reply(
            interaction,
            "Не удалось выполнить действие. Проверьте терминал.",
        )


class PublishView(SafeView):
    def __init__(self, service, user_id):
        super().__init__(timeout=300)

        self.service = service
        self.user_id = user_id

    async def interaction_check(self, interaction):
        allowed = (
            interaction.user.id == self.user_id
            and interaction.permissions.administrator
        )

        if not allowed:
            await reply(interaction, "Недостаточно прав.")

        return allowed

    @discord.ui.button(
        label="Разместить / обновить пять панелей",
        style=discord.ButtonStyle.success,
    )
    async def publish(self, interaction, button):
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            await self.service.publish_panels(interaction.guild)
        except Exception as error:
            logger.exception("Ошибка публикации панелей")
            await reply(
                interaction,
                f"Публикация не завершена: {error}",
            )
            return

        await reply(
            interaction,
            "Пять панелей размещены или обновлены.\n\n"
            "Сейчас проверяйте создание тикетов и обращения. "
            "Торговые каталоги и почтовые сделки ещё не подключены.",
        )


class TicketSystem:
    def __init__(self, client, database, root, read_config):
        self.client = client
        self.db = database
        self.root = Path(root)
        self.read_config = read_config

        self.templates = json.loads(
            (self.root / "data" / "templates.json").read_text(
                encoding="utf-8"
            )
        )

        self.creation_lock = asyncio.Lock()
        self.publish_lock = asyncio.Lock()
        self.locks = {}
        self.worker_task = None

    def lock(self, ticket_id):
        return self.locks.setdefault(ticket_id, asyncio.Lock())

    async def query_one(self, sql, parameters=()):
        return await asyncio.to_thread(
            self.db.fetchone,
            sql,
            parameters,
        )

    async def query_all(self, sql, parameters=()):
        return await asyncio.to_thread(
            self.db.fetchall,
            sql,
            parameters,
        )

    async def execute(self, sql, parameters=()):
        return await asyncio.to_thread(
            self.db.execute,
            sql,
            parameters,
        )

    async def config(self, guild_id):
        return await asyncio.to_thread(
            self.read_config,
            guild_id,
        )

    async def ticket_for(self, channel_id):
        return await self.query_one(
            "SELECT * FROM tickets WHERE channel_id=?",
            (channel_id,),
        )

    async def is_staff(self, member, ticket):
        if not isinstance(member, discord.Member):
            return False

        if member.guild.id != ticket["guild_id"]:
            return False

        if member.guild_permissions.administrator:
            return True

        config = await self.config(ticket["guild_id"])
        role_ids = set(
            config["roles"]["staff"][ticket["direction"]]
        )

        return any(role.id in role_ids for role in member.roles)

    async def channel(self, channel_id):
        channel = self.client.get_channel(channel_id)

        if channel is None:
            channel = await self.client.fetch_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Ожидался текстовый канал.")

        return channel

    async def publish_panels(self, guild):
        async with self.publish_lock:
            config = await self.config(guild.id)

            # Проверяем обязательные каналы до начала публикации.
            targets = {}

            for direction in DIRECTIONS:
                channel_id = config["channels"][direction]

                if not channel_id:
                    raise RuntimeError(
                        f"Не задан канал: {DIRECTIONS[direction]}."
                    )

                channel = await self.channel(channel_id)

                if channel.guild.id != guild.id:
                    raise RuntimeError("Выбран канал другого сервера.")

                targets[direction] = channel

            if len({ch.id for ch in targets.values()}) != 5:
                raise RuntimeError(
                    "Для пяти публичных панелей выберите "
                    "пять разных текстовых каналов."
                )

            for direction, channel in targets.items():
                panel_key = f"{guild.id}:{direction}"
                marker = f"StalPoint • panel:{direction}"

                if direction == "contact":
                    embed = discord.Embed(
                        title=self.templates["contact_panel_title"],
                        description=self.templates[
                            "contact_panel_description"
                        ],
                        color=discord.Color.blue(),
                    )
                else:
                    embed = discord.Embed(
                        title=DIRECTIONS[direction],
                        description=(
                            "Нажмите кнопку ниже — бот создаст "
                            "ваш приватный тикет.\n\n"
                            "**Тестирование:** торговые каталоги "
                            "и почтовые сделки пока не подключены."
                        ),
                        color=discord.Color.orange(),
                    )

                embed.set_footer(text=marker)

                view = PublicPanel(self, direction)

                saved = await self.query_one(
                    "SELECT * FROM panels WHERE panel_key=?",
                    (panel_key,),
                )

                message = None

                if saved and saved["message_id"]:
                    try:
                        old_channel = await self.channel(
                            saved["channel_id"]
                        )
                        old_message = await old_channel.fetch_message(
                            saved["message_id"]
                        )

                        if old_channel.id == channel.id:
                            message = old_message
                        else:
                            # При переносе панели старые кнопки
                            # больше не должны оставаться активными.
                            await old_message.edit(view=None)

                    except discord.NotFound:
                        pass

                if message is None:
                    # Восстановление при аварии между отправкой
                    # сообщения и записью его ID в SQLite.
                    async for candidate in channel.history(limit=None):
                        if candidate.author.id != self.client.user.id:
                            continue

                        if any(
                            item.footer.text == marker
                            for item in candidate.embeds
                        ):
                            message = candidate
                            break

                if message:
                    await message.edit(embed=embed, view=view)
                else:
                    message = await channel.send(
                        embed=embed,
                        view=view,
                    )

                await self.execute(
                    """
                    INSERT INTO panels(
                        panel_key, guild_id, channel_id,
                        message_id, state, updated_at
                    )
                    VALUES(?, ?, ?, ?, 'active', ?)
                    ON CONFLICT(panel_key) DO UPDATE SET
                        channel_id=excluded.channel_id,
                        message_id=excluded.message_id,
                        state='active',
                        updated_at=excluded.updated_at
                    """,
                    (
                        panel_key,
                        guild.id,
                        channel.id,
                        message.id,
                        utc_now(),
                    ),
                )

    async def create_ticket(self, interaction, direction, topic):
        if interaction.guild is None:
            await reply(interaction, "Используйте панель на сервере.")
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        guild = interaction.guild
        owner = interaction.user

        async with self.creation_lock:
            config = await self.config(guild.id)

            # Старое сообщение панели после смены настройки
            # не должно создавать тикеты.
            if (
                interaction.channel_id
                != config["channels"][direction]
            ):
                await reply(
                    interaction,
                    "Панель устарела. Используйте актуальный канал.",
                )
                return

            restriction = await self.query_one(
                """
                SELECT * FROM restrictions
                WHERE guild_id=? AND user_id=? AND direction=?
                """,
                (guild.id, owner.id, direction),
            )

            if restriction:
                expires = restriction["expires_at"]
                active = (
                    expires is None
                    or datetime.fromisoformat(expires)
                    > datetime.now(timezone.utc)
                )

                if active:
                    await reply(
                        interaction,
                        "Создание тикетов этого направления "
                        "для вас ограничено. Обратитесь к администрации.",
                    )
                    return

            open_tickets = await self.query_all(
                """
                SELECT * FROM tickets
                WHERE guild_id=? AND owner_id=?
                  AND state IN ('creating', 'open', 'closing')
                ORDER BY id
                """,
                (guild.id, owner.id),
            )

            same_direction = [
                item for item in open_tickets
                if item["direction"] == direction
            ]

            limits = config["limits"]

            if len(same_direction) >= limits["max_open_per_direction"]:
                existing = same_direction[0]

                if existing["channel_id"]:
                    await reply(
                        interaction,
                        "У вас уже есть тикет этого направления: "
                        f"<#{existing['channel_id']}>",
                    )
                else:
                    await reply(
                        interaction,
                        "Предыдущий тикет ещё создаётся. "
                        "Подождите немного.",
                    )
                return

            if len(open_tickets) >= limits["max_open_per_user"]:
                await reply(
                    interaction,
                    "Достигнут лимит ваших открытых тикетов.",
                )
                return

            total = await self.query_one(
                """
                SELECT COUNT(*) AS amount FROM tickets
                WHERE guild_id=?
                  AND state IN ('creating', 'open', 'closing')
                """,
                (guild.id,),
            )

            if total["amount"] >= limits["max_open_global"]:
                await reply(
                    interaction,
                    "Достигнут общий лимит открытых тикетов. "
                    "Попробуйте позднее.",
                )
                return

            previous = await self.query_one(
                """
                SELECT attempted_at FROM creation_attempts
                WHERE guild_id=? AND owner_id=? AND direction=?
                """,
                (guild.id, owner.id, direction),
            )

            if previous:
                seconds = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(previous["attempted_at"])
                ).total_seconds()

                remaining = (
                    limits["creation_cooldown_seconds"] - seconds
                )

                if remaining > 0:
                    await reply(
                        interaction,
                        f"Подождите ещё {int(remaining) + 1} сек.",
                    )
                    return

            category_id = config["categories"]["open"][direction]
            category = guild.get_channel(category_id)

            if not isinstance(category, discord.CategoryChannel):
                await reply(
                    interaction,
                    "Не настроена категория этого направления. "
                    "Администратору нужно проверить /admin.",
                )
                return

            if guild.me is None:
                await reply(interaction, "Не удалось получить роль бота.")
                return

            token = uuid.uuid4().hex
            now = utc_now()

            def prepare():
                with self.db.transaction() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO tickets(
                            guild_id, owner_id, direction, topic,
                            state, creation_token,
                            created_at, updated_at
                        )
                        VALUES(?, ?, ?, ?, 'creating', ?, ?, ?)
                        """,
                        (
                            guild.id,
                            owner.id,
                            direction,
                            topic,
                            token,
                            now,
                            now,
                        ),
                    )

                    connection.execute(
                        """
                        INSERT INTO creation_attempts(
                            guild_id, owner_id, direction, attempted_at
                        )
                        VALUES(?, ?, ?, ?)
                        ON CONFLICT(guild_id, owner_id, direction)
                        DO UPDATE SET
                            attempted_at=excluded.attempted_at
                        """,
                        (guild.id, owner.id, direction, now),
                    )

                    return cursor.lastrowid

            ticket_id = await asyncio.to_thread(prepare)

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=False,
                ),
                owner: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    mention_everyone=False,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    manage_channels=True,
                ),
            }

            for role_id in config["roles"]["staff"][direction]:
                role = guild.get_role(role_id)

                if role and not role.is_default():
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                        embed_links=True,
                    )

            try:
                channel = await guild.create_text_channel(
                    name=f"{direction}-{ticket_id:05d}",
                    category=category,
                    topic=f"stalpoint:{token}",
                    overwrites=overwrites,
                    reason=f"Тикет №{ticket_id}; пользователь {owner.id}",
                )
            except discord.HTTPException:
                # Сохраняем creating: при сетевой неопределённости
                # канал мог быть создан. Восстановление проверит token.
                await reply(
                    interaction,
                    "Discord не подтвердил создание канала. "
                    "Бот проверит результат автоматически. "
                    "Проверьте права бота и ограничения категории.",
                )
                return

            await self.execute(
                """
                UPDATE tickets
                SET channel_id=?, updated_at=?
                WHERE id=?
                """,
                (channel.id, utc_now(), ticket_id),
            )

            ticket = await self.query_one(
                "SELECT * FROM tickets WHERE id=?",
                (ticket_id,),
            )

            try:
                await self.finish_creation(ticket, channel)
            except Exception:
                logger.exception(
                    "Ошибка оформления тикета %s",
                    ticket_id,
                )

                await reply(
                    interaction,
                    f"Канал создан: {channel.mention}. "
                    "Оформление будет повторено автоматически.",
                )
                return

            await reply(
                interaction,
                f"Ваш тикет №{ticket_id}: {channel.mention}",
            )

    async def finish_creation(self, ticket, channel):
        config = await self.config(ticket["guild_id"])
        direction = ticket["direction"]

        marker = f"StalPoint • ticket:{ticket['id']}"

        interface = None

        async for message in channel.history(limit=None):
            if message.author.id != self.client.user.id:
                continue

            if any(
                embed.footer.text == marker
                for embed in message.embeds
            ):
                interface = message
                break

        if interface is None:
            if direction == "contact":
                faq = await self.query_one(
                    "SELECT * FROM faq_topics WHERE topic_id=?",
                    (ticket["topic"],),
                )

                if not faq:
                    raise RuntimeError("Тема FAQ отсутствует в базе.")

                text = self.templates["contact_greeting"].format(
                    owner=f"<@{ticket['owner_id']}>",
                    topic=faq["label"],
                    faq=faq["answer"],
                )

            elif direction == "resources":
                text = self.templates["resources_intro"].format(
                    recipient=config["mail"]["resources"]["recipient"],
                )

            elif direction == "pawn":
                text = self.templates["pawn_intro"].format(
                    recipient=config["mail"]["pawn"]["recipient"],
                )

            elif direction == "boosts":
                text = self.templates["boosts_intro"].format(
                    minimum=f"{config['limits']['boosts_minimum']:,}".replace(
                        ",", " "
                    ),
                    currency=config["currency"]["boosts"],
                )

            else:
                status = await asyncio.to_thread(
                    self.db.setting_get,
                    "arsenal_seller_status",
                    "inactive",
                )

                text = self.templates["arsenal_intro"].format(
                    reputation_per_limit=config["arsenal"][
                        "reputation_per_limit"
                    ],
                    seller_status=(
                        "Актив" if status == "active" else "Инактив"
                    ),
                )

            if direction != "contact":
                text += (
                    "\n\n**Тестирование проекта:** каталог, корзина "
                    "и почтовый сценарий ещё не подключены. "
                    "Не отправляйте предметы на основании "
                    "этого тестового тикета."
                )

            # Сохраняем полный текст, даже если он длиннее embed.
            chunks = list(split_text(text, 3500))

            for chunk in chunks[:-1]:
                await channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            embed = discord.Embed(
                title=(
                    f"Тикет №{ticket['id']} • "
                    f"{DIRECTIONS[direction]}"
                ),
                description=chunks[-1],
                color=discord.Color.blue(),
            )
            embed.set_footer(text=marker)

            interface = await channel.send(
                content=f"<@{ticket['owner_id']}>",
                embed=embed,
                view=TicketControls(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await self.execute(
            """
            UPDATE tickets
            SET interface_message_id=?, state='open',
                last_error=NULL, updated_at=?
            WHERE id=? AND state='creating'
            """,
            (interface.id, utc_now(), ticket["id"]),
        )

    async def handle_message(self, message):
        if message.guild is None:
            return

        ticket = await self.ticket_for(message.channel.id)

        if not ticket:
            return

        await self.execute(
            """
            INSERT INTO messages(
                message_id, ticket_id, author_id, author_name,
                is_bot, content, created_at, edited_at,
                attachments_json, embeds_json, jump_url
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                content=excluded.content,
                edited_at=excluded.edited_at,
                attachments_json=excluded.attachments_json,
                embeds_json=excluded.embeds_json
            """,
            (
                message.id,
                ticket["id"],
                message.author.id,
                str(message.author),
                int(message.author.bot),
                message.content,
                message.created_at.isoformat(),
                (
                    message.edited_at.isoformat()
                    if message.edited_at else None
                ),
                json_dump([
                    {
                        "id": item.id,
                        "filename": item.filename,
                        "url": item.url,
                        "size": item.size,
                    }
                    for item in message.attachments
                ]),
                json_dump([
                    embed.to_dict()
                    for embed in message.embeds
                ]),
                message.jump_url,
            ),
        )

        if (
            ticket["state"] != "open"
            or message.author.bot
            or message.author.id != ticket["owner_id"]
            or not (message.content.strip() or message.attachments)
            or ticket["direction"] not in {"contact", "pawn"}
        ):
            return

        async with self.lock(ticket["id"]):
            await self.execute(
                """
                INSERT OR IGNORE INTO notifications(
                    ticket_id, notification_key, state,
                    source_message_id, payload_json,
                    created_at, updated_at
                )
                VALUES(?, 'first_message', 'pending', ?, ?, ?, ?)
                """,
                (
                    ticket["id"],
                    message.id,
                    json_dump({"message_url": message.jump_url}),
                    utc_now(),
                    utc_now(),
                ),
            )

            await self.deliver_notification(ticket)

    async def deliver_notification(self, ticket):
        notification = await self.query_one(
            """
            SELECT * FROM notifications
            WHERE ticket_id=? AND notification_key='first_message'
            """,
            (ticket["id"],),
        )

        if not notification or notification["state"] == "sent":
            return

        channel = await self.channel(ticket["channel_id"])
        config = await self.config(ticket["guild_id"])

        key = (
            ticket["topic"]
            if ticket["direction"] == "contact"
            else "pawn"
        )

        role_ids = config["roles"]["notifications"].get(key, [])

        roles = [
            channel.guild.get_role(role_id)
            for role_id in role_ids
        ]
        roles = [
            role for role in roles
            if role and not role.is_default()
        ]

        mentions = " ".join(role.mention for role in roles)

        if not mentions:
            mentions = "Сотрудники"

        payload = json.loads(notification["payload_json"])

        if ticket["direction"] == "contact":
            topic = TOPICS[ticket["topic"]][1]

            text = self.templates["contact_notification"].format(
                roles=mentions,
                topic=topic,
                message_url=payload["message_url"],
            )
        else:
            text = self.templates["pawn_first_message"].format(
                roles=mentions,
                message_url=payload["message_url"],
            )

        marker = f"notification:{notification['id']}"

        # Проверяем отправленное сообщение при восстановлении
        # после аварии до записи message_id.
        delivered = None

        async for candidate in channel.history(limit=100):
            if candidate.author.id != self.client.user.id:
                continue

            if any(
                embed.footer.text == marker
                for embed in candidate.embeds
            ):
                delivered = candidate
                break

        if delivered is None:
            embed = discord.Embed(
                description="Обращение передано сотрудникам.",
                color=discord.Color.orange(),
            )
            embed.set_footer(text=marker)

            delivered = await channel.send(
                content=text,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=roles,
                    replied_user=False,
                ),
            )

        await self.execute(
            """
            UPDATE notifications
            SET state='sent', delivered_message_id=?,
                last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (
                delivered.id,
                utc_now(),
                notification["id"],
            ),
        )

    async def close_ticket(self, ticket, actor_id, reason):
        async with self.lock(ticket["id"]):
            current = await self.query_one(
                "SELECT * FROM tickets WHERE id=?",
                (ticket["id"],),
            )

            if current["state"] in {"closed", "missing"}:
                return

            if current["state"] not in {"open", "closing"}:
                raise RuntimeError("Тикет ещё создаётся.")

            config = await self.config(ticket["guild_id"])
            mode = config["closing"]["mode"]

            if mode not in {"move", "delete"}:
                raise RuntimeError("Неизвестный режим закрытия.")

            if not config["channels"]["transcripts"]:
                raise RuntimeError("Не настроен канал истории.")

            if mode == "move":
                guild = self.client.get_guild(ticket["guild_id"])

                category = guild.get_channel(
                    config["categories"]["closed"]
                ) if guild else None

                if not isinstance(category, discord.CategoryChannel):
                    raise RuntimeError(
                        "Не настроена категория закрытых тикетов."
                    )

            now = utc_now()

            def prepare():
                with self.db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO close_jobs(
                            ticket_id, kind, state, reason,
                            actor_id, snapshot_json, close_mode,
                            created_at, updated_at
                        )
                        VALUES(?, 'manual', 'prepared', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ticket["id"],
                            reason,
                            actor_id,
                            json_dump(current),
                            mode,
                            now,
                            now,
                        ),
                    )

                    connection.execute(
                        """
                        UPDATE tickets
                        SET state='closing', updated_at=?
                        WHERE id=? AND state='open'
                        """,
                        (now, ticket["id"]),
                    )

            await asyncio.to_thread(prepare)
            await self.continue_close(ticket["id"])

    async def continue_close(self, ticket_id):
        ticket = await self.query_one(
            "SELECT * FROM tickets WHERE id=?",
            (ticket_id,),
        )
        job = await self.query_one(
            "SELECT * FROM close_jobs WHERE ticket_id=?",
            (ticket_id,),
        )

        if not job or job["state"] == "closed":
            return

        config = await self.config(ticket["guild_id"])

        if job["state"] == "prepared":
            channel = await self.channel(ticket["channel_id"])

            # Считываем полную доступную историю Discord.
            # Нельзя считать ранее записанные события единственным
            # источником: бот мог быть выключен.
            transcript = [
                f"Тикет №{ticket_id}",
                f"Направление: {ticket['direction']}",
                f"Владелец: {ticket['owner_id']}",
                f"Причина: {job['reason']}",
                "",
                self.templates["attachment_warning"],
                "",
            ]

            async for message in channel.history(
                limit=None,
                oldest_first=True,
            ):
                transcript.append(
                    json.dumps(
                        {
                            "id": message.id,
                            "author_id": message.author.id,
                            "author_name": str(message.author),
                            "created_at": message.created_at.isoformat(),
                            "edited_at": (
                                message.edited_at.isoformat()
                                if message.edited_at else None
                            ),
                            "content": message.content,
                            "attachments": [
                                {
                                    "filename": item.filename,
                                    "url": item.url,
                                    "size": item.size,
                                }
                                for item in message.attachments
                            ],
                            "embeds": [
                                embed.to_dict()
                                for embed in message.embeds
                            ],
                            "jump_url": message.jump_url,
                        },
                        ensure_ascii=False,
                    )
                )

            text = "\n".join(transcript)
            directory = self.root / config["exports_directory"]
            directory.mkdir(parents=True, exist_ok=True)

            # Максимум 1 млн символов в части:
            # даже для четырёхбайтного UTF-8 это менее 4 МБ.
            parts = list(split_text(text, 1_000_000))
            paths = []

            for index, part in enumerate(parts):
                path = directory / (
                    f"ticket-{ticket_id}-part-{index + 1}.txt"
                )

                temporary = path.with_suffix(".tmp")
                await asyncio.to_thread(
                    temporary.write_text,
                    part,
                    encoding="utf-8",
                )
                await asyncio.to_thread(temporary.replace, path)

                paths.append(str(path.resolve()))

            await self.execute(
                """
                UPDATE close_jobs
                SET state='history_saved',
                    transcript_manifest_json=?,
                    updated_at=?, last_error=NULL
                WHERE ticket_id=?
                """,
                (json_dump(paths), utc_now(), ticket_id),
            )

            job = await self.query_one(
                "SELECT * FROM close_jobs WHERE ticket_id=?",
                (ticket_id,),
            )

        if job["state"] == "history_saved":
            log_channel = await self.channel(
                config["channels"]["transcripts"]
            )

            paths = json.loads(job["transcript_manifest_json"])

            for index, filename in enumerate(paths):
                delivery = await self.query_one(
                    """
                    SELECT * FROM transcript_deliveries
                    WHERE ticket_id=? AND part_index=?
                    """,
                    (ticket_id, index),
                )

                if delivery and delivery["state"] == "sent":
                    continue

                marker = (
                    f"StalPoint history:{ticket_id}:{index}"
                )

                delivered = None

                async for candidate in log_channel.history(limit=200):
                    if (
                        candidate.author.id == self.client.user.id
                        and candidate.content == marker
                    ):
                        delivered = candidate
                        break

                if delivered is None:
                    data = await asyncio.to_thread(
                        Path(filename).read_bytes
                    )

                    delivered = await log_channel.send(
                        content=marker,
                        file=discord.File(
                            io.BytesIO(data),
                            filename=Path(filename).name,
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )

                await self.execute(
                    """
                    INSERT INTO transcript_deliveries(
                        ticket_id, part_index, message_id, state
                    )
                    VALUES(?, ?, ?, 'sent')
                    ON CONFLICT(ticket_id, part_index)
                    DO UPDATE SET
                        message_id=excluded.message_id,
                        state='sent',
                        last_error=NULL
                    """,
                    (ticket_id, index, delivered.id),
                )

            await self.execute(
                """
                UPDATE close_jobs
                SET state='history_sent',
                    history_complete_at=?, updated_at=?,
                    last_error=NULL
                WHERE ticket_id=?
                """,
                (utc_now(), utc_now(), ticket_id),
            )

            job = await self.query_one(
                "SELECT * FROM close_jobs WHERE ticket_id=?",
                (ticket_id,),
            )

        if job["state"] == "history_sent":
            try:
                channel = await self.channel(ticket["channel_id"])
            except discord.NotFound:
                channel = None

            if channel is not None:
                if job["close_mode"] == "delete":
                    await asyncio.sleep(
                        config["closing"]["delete_delay_seconds"]
                    )

                    try:
                        await channel.delete(
                            reason=f"Закрытие тикета №{ticket_id}",
                        )
                    except discord.NotFound:
                        pass

                else:
                    category = channel.guild.get_channel(
                        config["categories"]["closed"]
                    )

                    if not isinstance(
                        category,
                        discord.CategoryChannel,
                    ):
                        raise RuntimeError(
                            "Категория закрытых тикетов недоступна."
                        )

                    overwrites = channel.overwrites

                    for target, overwrite in list(overwrites.items()):
                        if target.id == self.client.user.id:
                            continue

                        overwrite.send_messages = False
                        overwrite.send_messages_in_threads = False
                        overwrite.create_public_threads = False
                        overwrite.create_private_threads = False

                        overwrites[target] = overwrite

                    await channel.edit(
                        category=category,
                        overwrites=overwrites,
                        reason=f"Закрытие тикета №{ticket_id}",
                    )

            def finish():
                with self.db.transaction() as connection:
                    now = utc_now()

                    connection.execute(
                        """
                        UPDATE close_jobs
                        SET state='closed', channel_closed_at=?,
                            updated_at=?, last_error=NULL
                        WHERE ticket_id=?
                        """,
                        (now, now, ticket_id),
                    )

                    connection.execute(
                        """
                        UPDATE tickets
                        SET state='closed', staff_status='closed',
                            closed_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (now, now, ticket_id),
                    )

                    connection.execute(
                        "DELETE FROM message_waits WHERE ticket_id=?",
                        (ticket_id,),
                    )

            await asyncio.to_thread(finish)

    async def recover(self):
        guilds = {}

        creating = await self.query_all(
            "SELECT * FROM tickets WHERE state='creating'"
        )

        for ticket in creating:
            try:
                guild = self.client.get_guild(ticket["guild_id"])

                if guild is None:
                    continue

                if guild.id not in guilds:
                    guilds[guild.id] = await guild.fetch_channels()

                marker = f"stalpoint:{ticket['creation_token']}"

                channel = next(
                    (
                        item for item in guilds[guild.id]
                        if isinstance(item, discord.TextChannel)
                        and item.topic == marker
                    ),
                    None,
                )

                if channel:
                    await self.execute(
                        """
                        UPDATE tickets
                        SET channel_id=?, updated_at=?
                        WHERE id=?
                        """,
                        (channel.id, utc_now(), ticket["id"]),
                    )

                    await self.finish_creation(ticket, channel)

                else:
                    age = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(ticket["created_at"])
                    ).total_seconds()

                    if age > 180:
                        await self.execute(
                            """
                            UPDATE tickets
                            SET state='creation_failed',
                                last_error=?, updated_at=?
                            WHERE id=? AND state='creating'
                            """,
                            (
                                "Канал не найден при восстановлении.",
                                utc_now(),
                                ticket["id"],
                            ),
                        )

            except Exception:
                logger.exception(
                    "Не восстановлено создание тикета %s",
                    ticket["id"],
                )

        closing = await self.query_all(
            """
            SELECT ticket_id FROM close_jobs
            WHERE state!='closed'
            """
        )

        for row in closing:
            ticket_id = row["ticket_id"]

            async with self.lock(ticket_id):
                try:
                    await self.continue_close(ticket_id)
                except Exception as error:
                    logger.exception(
                        "Не восстановлено закрытие %s",
                        ticket_id,
                    )

                    await self.execute(
                        """
                        UPDATE close_jobs
                        SET last_error=?, attempts=attempts+1,
                            updated_at=?
                        WHERE ticket_id=?
                        """,
                        (str(error), utc_now(), ticket_id),
                    )

        pending = await self.query_all(
            """
            SELECT DISTINCT tickets.*
            FROM notifications
            JOIN tickets ON tickets.id=notifications.ticket_id
            WHERE notifications.state='pending'
              AND tickets.state='open'
            """
        )

        for ticket in pending:
            async with self.lock(ticket["id"]):
                try:
                    await self.deliver_notification(ticket)
                except Exception:
                    logger.exception(
                        "Не доставлено уведомление тикета %s",
                        ticket["id"],
                    )

    async def worker(self):
        await self.client.wait_until_ready()

        while not self.client.is_closed():
            try:
                async with self.creation_lock:
                    await self.recover()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка восстановления")

            await asyncio.sleep(60)

    def register(self):
        for direction in DIRECTIONS:
            self.client.add_view(PublicPanel(self, direction))

        self.client.add_view(TicketControls(self))

        @self.client.tree.command(
            name="panels",
            description="Первичная публикация и обновление панелей",
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        @app_commands.checks.has_permissions(administrator=True)
        async def panels(interaction: discord.Interaction):
            await interaction.response.send_message(
                "Настройки берутся из SQLite — повторно "
                "выбирать каналы не нужно.\n\n"
                "**Это тестовый этап:** торговые каталоги "
                "и почтовые сделки ещё не подключены.",
                view=PublishView(self, interaction.user.id),
                ephemeral=True,
            )