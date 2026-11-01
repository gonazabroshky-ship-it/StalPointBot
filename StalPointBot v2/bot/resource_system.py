from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math

import discord
from discord import app_commands

from bot.database import (
    ValidationError,
    json_dump,
    positive_integer,
    utc_now,
)
from bot.ticket_system import (
    SafeView,
    TicketControls,
    TicketSystem,
    reply,
    split_text,
)


log = logging.getLogger("stalpoint.resources")

STATUSES = {
    "new": "Ожидает получения ресурсов",
    "in_progress": "Ресурсы получены и проверены",
    "completed": "Выплата подтверждена сотрудником",
    "cancelled": "Отменён",
}


def number(value):
    return f"{value:,}".replace(",", " ")


def fingerprint(value):
    return hashlib.sha256(
        json_dump(value).encode("utf-8")
    ).hexdigest()


def summary(order):
    snapshot = json.loads(order["snapshot_json"])

    lines = [
        f"Заявка №{order['id']}",
        f"Игровой ник: {order['game_nickname']}",
        "",
    ]

    for item in snapshot["items"]:
        lines.append(
            f"• {item['name']}: "
            f"{number(item['quantity'])} × "
            f"{number(item['price'])} = "
            f"{number(item['subtotal'])}"
        )

    lines += [
        "",
        f"Итого: {number(order['total'])} "
        f"{order['currency']}.",
        f"Статус: {STATUSES[order['status']]}.",
        "",
        "Расчёт не подтверждает получение ресурсов или выплату.",
    ]

    return "\n".join(lines)


async def send_text(interaction, text):
    for part in split_text(text):
        if interaction.response.is_done():
            await interaction.followup.send(
                part,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                part,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class Form(discord.ui.Modal):
    """Формы с единым обработчиком ошибок."""

    def __init__(self, title, fields, callback):
        super().__init__(title=title, timeout=300)
        self.callback_function = callback
        self.inputs = []

        for options in fields:
            field = discord.ui.TextInput(**options)
            self.inputs.append(field)
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            await self.callback_function(
                interaction,
                [item.value for item in self.inputs],
            )
        except ValidationError as error:
            await reply(interaction, str(error))

    async def on_error(self, interaction, error):
        log.error(
            "Ошибка формы ресурсов",
            exc_info=(type(error), error, error.__traceback__),
        )

        await reply(
            interaction,
            "Действие не завершено. Подробности — в терминале. "
            "Перед повторением проверьте корзину или статус заявки.",
        )


class ResourceControls(TicketControls):
    def __init__(self, service):
        super().__init__(service)

        # Оставляем штатные кнопки сотрудника.
        for item in list(self.children):
            if item.custom_id in {
                "stalpoint:ticket:help:v1",
                "stalpoint:ticket:faq_close:v1",
            }:
                self.remove_item(item)

        buttons = [
            (
                "Каталог",
                "catalog",
                discord.ButtonStyle.primary,
                self.catalog,
            ),
            (
                "Корзина / заявка",
                "cart",
                discord.ButtonStyle.secondary,
                self.cart,
            ),
            (
                "Подтвердить заявку",
                "confirm",
                discord.ButtonStyle.success,
                self.confirm,
            ),
            (
                "Почта / повторить отправку",
                "mail",
                discord.ButtonStyle.primary,
                self.mail,
            ),
            (
                "Почему нам доверяют",
                "trust",
                discord.ButtonStyle.secondary,
                self.trust,
            ),
        ]

        for label, key, style, callback in buttons:
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"stalpoint:resources:{key}:v1",
            )
            button.callback = callback
            self.add_item(button)

    async def catalog(self, interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = await self.service.owner_ticket(interaction)
        await self.service.show_catalog(interaction, ticket)

    async def cart(self, interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = await self.service.owner_ticket(interaction)
        await self.service.show_cart(interaction, ticket)

    async def confirm(self, interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = await self.service.owner_ticket(interaction)
        await self.service.show_confirmation(interaction, ticket)

    async def mail(self, interaction):
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )
        ticket = await self.service.owner_ticket(
            interaction,
            allow_closing=True,
        )
        await self.service.start_mail(interaction, ticket)

    async def trust(self, interaction):
        await send_text(
            interaction,
            self.service.templates["trust_explanation"],
        )

    async def on_error(self, interaction, error, item):
        if isinstance(error, ValidationError):
            await reply(interaction, str(error))
            return

        await super().on_error(interaction, error, item)


class CatalogView(SafeView):
    def __init__(self, service, user_id, page, pages, products, query):
        super().__init__(timeout=600)

        self.service = service
        self.user_id = user_id
        self.page = page
        self.query = query

        if products:
            menu = discord.ui.Select(
                placeholder="Выберите ресурс и укажите количество",
                options=[
                    discord.SelectOption(
                        label=product["name"][:100],
                        value=product["item_id"],
                        description=(
                            f"{number(product['price'])} за шт. • "
                            f"{product['category']}"
                        )[:100],
                    )
                    for product in products
                ],
                row=0,
            )
            menu.callback = self.choose
            self.add_item(menu)

        previous = discord.ui.Button(
            label="Назад",
            disabled=page == 0,
            row=1,
        )
        previous.callback = self.previous
        self.add_item(previous)

        following = discord.ui.Button(
            label="Далее",
            disabled=page >= pages - 1,
            row=1,
        )
        following.callback = self.following
        self.add_item(following)

        search = discord.ui.Button(
            label="Поиск",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        search.callback = self.search
        self.add_item(search)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await reply(interaction, "Это меню другого пользователя.")
            return False
        return True

    async def choose(self, interaction):
        item_id = interaction.data["values"][0]

        async def apply(submission, values):
            ticket = await self.service.owner_ticket(submission)
            config = await self.service.config(ticket["guild_id"])

            quantity = positive_integer(
                values[0],
                config["limits"]["max_quantity"],
            )

            await self.service.transaction(
                lambda connection: self.service.set_quantity(
                    connection,
                    ticket,
                    item_id,
                    quantity,
                    config,
                )
            )

            await reply(
                submission,
                "Количество сохранено. Повторный выбор этого ресурса "
                "заменит количество, а не прибавит его.\n"
                "Проверить состав можно кнопкой «Корзина / заявка».",
            )

        await interaction.response.send_modal(
            Form(
                "Количество ресурса",
                [
                    {
                        "label": "Количество — целое число",
                        "placeholder": "Например: 100",
                        "max_length": 20,
                        "required": True,
                    }
                ],
                apply,
            )
        )

    async def previous(self, interaction):
        await self.change_page(interaction, self.page - 1)

    async def following(self, interaction):
        await self.change_page(interaction, self.page + 1)

    async def change_page(self, interaction, page):
        await interaction.response.defer(ephemeral=True)
        ticket = await self.service.owner_ticket(interaction)
        await self.service.show_catalog(
            interaction,
            ticket,
            page=page,
            query=self.query,
        )

    async def search(self, interaction):
        async def apply(submission, values):
            ticket = await self.service.owner_ticket(submission)
            await self.service.show_catalog(
                submission,
                ticket,
                query=values[0].strip(),
            )

        await interaction.response.send_modal(
            Form(
                "Поиск ресурсов",
                [
                    {
                        "label": "Часть названия; пусто — весь каталог",
                        "required": False,
                        "max_length": 100,
                    }
                ],
                apply,
            )
        )

    async def on_error(self, interaction, error, item):
        if isinstance(error, ValidationError):
            await reply(interaction, str(error))
            return
        await super().on_error(interaction, error, item)


class RemoveView(SafeView):
    def __init__(self, service, user_id, items):
        super().__init__(timeout=300)
        self.service = service
        self.user_id = user_id

        menu = discord.ui.Select(
            placeholder="Удалить позицию из корзины",
            options=[
                discord.SelectOption(
                    label=item["name"][:100],
                    value=item["item_id"],
                )
                for item in items
            ],
        )
        menu.callback = self.remove
        self.add_item(menu)

    async def remove(self, interaction):
        if interaction.user.id != self.user_id:
            await reply(interaction, "Это меню другого пользователя.")
            return

        await interaction.response.defer(ephemeral=True)
        ticket = await self.service.owner_ticket(interaction)
        item_id = interaction.data["values"][0]

        def apply(connection):
            self.service.check_editable(connection, ticket)
            connection.execute(
                """
                DELETE FROM cart_items
                WHERE ticket_id=? AND catalog='resources'
                  AND item_id=?
                """,
                (ticket["id"], item_id),
            )
            self.service.bump_cart(connection, ticket["id"])

        await self.service.transaction(apply)
        await reply(
            interaction,
            "Позиция удалена. Откройте корзину заново "
            "для просмотра актуального состава.",
        )

    async def on_error(self, interaction, error, item):
        if isinstance(error, ValidationError):
            await reply(interaction, str(error))
            return
        await super().on_error(interaction, error, item)


class ConfirmView(SafeView):
    def __init__(self, service, user_id, expected):
        super().__init__(timeout=300)
        self.service = service
        self.user_id = user_id
        self.expected = expected

    @discord.ui.button(
        label="Подтвердить этот расчёт",
        style=discord.ButtonStyle.success,
    )
    async def confirm(self, interaction, button):
        if interaction.user.id != self.user_id:
            await reply(interaction, "Это расчёт другого пользователя.")
            return

        async def apply(submission, values):
            ticket = await self.service.owner_ticket(submission)
            nickname = values[0].strip()

            if not 1 <= len(nickname) <= 64:
                raise ValidationError("Укажите игровой ник: 1–64 символа.")

            if any(ord(char) < 32 for char in nickname):
                raise ValidationError("Ник не должен содержать переносы строк.")

            config = await self.service.config(ticket["guild_id"])

            order_id = await self.service.transaction(
                lambda connection: self.service.create_order(
                    connection,
                    ticket,
                    nickname,
                    config,
                    self.expected,
                )
            )

            await reply(
                submission,
                f"Заявка №{order_id} сохранена.\n\n"
                "Далее нажмите в тикете "
                "«Почта / повторить отправку». "
                "Бот отправит инструкции в личные сообщения, "
                "сохранит историю и закроет тикет.\n\n"
                "Заявка останется доступна сотрудникам.",
            )

        await interaction.response.send_modal(
            Form(
                "Подтверждение заявки",
                [
                    {
                        "label": "Ваш точный игровой ник",
                        "max_length": 64,
                        "required": True,
                    }
                ],
                apply,
            )
        )

    async def on_error(self, interaction, error, item):
        if isinstance(error, ValidationError):
            await reply(interaction, str(error))
            return
        await super().on_error(interaction, error, item)


class StaffOrderView(SafeView):
    def __init__(self, service, order):
        super().__init__(timeout=300)
        self.service = service
        self.order = order

    @discord.ui.button(
        label="Изменить статус заявки",
        style=discord.ButtonStyle.primary,
    )
    async def update(self, interaction, button):
        await self.service.require_resource_staff(interaction)

        async def apply(submission, values):
            await self.service.require_resource_staff(submission)

            action = values[0].strip().upper()
            reason = values[1].strip()

            actions = {
                "ПОЛУЧЕНО": "in_progress",
                "ОПЛАЧЕНО": "completed",
                "ОТМЕНА": "cancelled",
            }

            if action not in actions:
                raise ValidationError(
                    "Допустимо: ПОЛУЧЕНО, ОПЛАЧЕНО или ОТМЕНА."
                )

            if not reason:
                raise ValidationError("Укажите пояснение.")

            status = actions[action]

            def change(connection):
                order = connection.execute(
                    "SELECT * FROM orders WHERE id=?",
                    (self.order["id"],),
                ).fetchone()

                if (
                    not order
                    or order["guild_id"] != submission.guild_id
                    or order["direction"] != "resources"
                ):
                    raise ValidationError("Заявка не найдена.")

                if order["version"] != self.order["version"]:
                    raise ValidationError(
                        "Заявка уже изменена. Откройте её заново."
                    )

                if order["status"] in {"completed", "cancelled"}:
                    raise ValidationError(
                        "Заявка уже завершена или отменена."
                    )

                if status == "in_progress" and order["status"] != "new":
                    raise ValidationError(
                        "Получение ресурсов уже отмечено."
                    )

                if status == "completed" and order["status"] != "in_progress":
                    raise ValidationError(
                        "Сначала подтвердите получение и проверку "
                        "ресурсов действием ПОЛУЧЕНО."
                    )

                now = utc_now()

                connection.execute(
                    """
                    UPDATE orders
                    SET status=?, version=version+1, updated_at=?
                    WHERE id=?
                    """,
                    (status, now, order["id"]),
                )

                connection.execute(
                    """
                    INSERT INTO order_events(
                        order_id, actor_id, old_status,
                        new_status, reason, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order["id"],
                        submission.user.id,
                        order["status"],
                        status,
                        reason,
                        now,
                    ),
                )

            await self.service.transaction(change)

            await reply(
                submission,
                f"Заявка №{self.order['id']}: {STATUSES[status]}.\n"
                "Действие и пояснение сохранены в базе.",
            )

        await interaction.response.send_modal(
            Form(
                "Статус сделки — подтверждает сотрудник",
                [
                    {
                        "label": "ПОЛУЧЕНО / ОПЛАЧЕНО / ОТМЕНА",
                        "max_length": 20,
                        "required": True,
                    },
                    {
                        "label": "Пояснение или причина",
                        "style": discord.TextStyle.paragraph,
                        "max_length": 500,
                        "required": True,
                    },
                ],
                apply,
            )
        )

    async def on_error(self, interaction, error, item):
        if isinstance(error, ValidationError):
            await reply(interaction, str(error))
            return
        await super().on_error(interaction, error, item)


class ResourceSystem(TicketSystem):
    async def transaction(self, function):
        def execute():
            with self.db.transaction() as connection:
                return function(connection)

        return await asyncio.to_thread(execute)

    async def owner_ticket(self, interaction, allow_closing=False):
        ticket = await self.ticket_for(interaction.channel_id)

        if (
            not ticket
            or ticket["guild_id"] != interaction.guild_id
            or ticket["direction"] != "resources"
            or ticket["owner_id"] != interaction.user.id
        ):
            raise ValidationError(
                "Действие доступно владельцу ресурсного тикета."
            )

        states = {"open", "closing"} if allow_closing else {"open"}

        if ticket["state"] not in states:
            raise ValidationError(
                "Тикет закрыт или сейчас закрывается."
            )

        return ticket

    def check_editable(self, connection, ticket):
        current = connection.execute(
            "SELECT * FROM tickets WHERE id=?",
            (ticket["id"],),
        ).fetchone()

        if not current or current["state"] != "open":
            raise ValidationError("Тикет уже закрывается или закрыт.")

        if connection.execute(
            "SELECT id FROM orders WHERE ticket_id=?",
            (ticket["id"],),
        ).fetchone():
            raise ValidationError(
                "Заявка уже подтверждена. Корзину менять нельзя. "
                "Для изменения условий обратитесь к сотруднику."
            )

    def bump_cart(self, connection, ticket_id):
        connection.execute(
            """
            INSERT INTO cart_meta(ticket_id, revision, updated_at)
            VALUES(?, 1, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                revision=revision+1,
                acknowledged_fingerprint=NULL,
                updated_at=excluded.updated_at
            """,
            (ticket_id, utc_now()),
        )

    def set_quantity(
        self,
        connection,
        ticket,
        item_id,
        quantity,
        config,
    ):
        self.check_editable(connection, ticket)

        product = connection.execute(
            """
            SELECT * FROM products
            WHERE catalog='resources' AND item_id=?
            """,
            (item_id,),
        ).fetchone()

        if not product or not product["available"]:
            raise ValidationError("Этот ресурс сейчас не принимается.")

        exists = connection.execute(
            """
            SELECT 1 FROM cart_items
            WHERE ticket_id=? AND catalog='resources' AND item_id=?
            """,
            (ticket["id"], item_id),
        ).fetchone()

        count = connection.execute(
            "SELECT COUNT(*) FROM cart_items WHERE ticket_id=?",
            (ticket["id"],),
        ).fetchone()[0]

        if not exists and count >= config["limits"]["max_cart_positions"]:
            raise ValidationError("Достигнут лимит позиций в корзине.")

        connection.execute(
            """
            INSERT INTO cart_items(
                ticket_id, catalog, item_id, quantity_text,
                seen_price, seen_version
            )
            VALUES(?, 'resources', ?, ?, ?, ?)
            ON CONFLICT(ticket_id, catalog, item_id) DO UPDATE SET
                quantity_text=excluded.quantity_text,
                seen_price=excluded.seen_price,
                seen_version=excluded.seen_version
            """,
            (
                ticket["id"],
                item_id,
                str(quantity),
                product["price"],
                product["version"],
            ),
        )

        self.bump_cart(connection, ticket["id"])

    def calculation(self, connection, ticket_id, config):
        rows = connection.execute(
            """
            SELECT p.*, c.quantity_text
            FROM cart_items AS c
            JOIN products AS p
              ON p.catalog=c.catalog AND p.item_id=c.item_id
            WHERE c.ticket_id=? AND c.catalog='resources'
            ORDER BY p.sort_order, p.item_id
            """,
            (ticket_id,),
        ).fetchall()

        if not rows:
            raise ValidationError(
                "Корзина пуста. Сначала выберите ресурсы в каталоге."
            )

        if len(rows) > config["limits"]["max_cart_positions"]:
            raise ValidationError("Превышен текущий лимит позиций.")

        items = []
        total = 0

        for row in rows:
            if not row["available"]:
                raise ValidationError(
                    f"«{row['name']}» больше не принимается. "
                    "Удалите эту позицию из корзины."
                )

            quantity = positive_integer(
                row["quantity_text"],
                config["limits"]["max_quantity"],
            )

            if not 0 < row["price"] <= config["limits"]["max_price"]:
                raise ValidationError(
                    f"Цена «{row['name']}» вне допустимых ограничений."
                )

            subtotal = quantity * row["price"]
            total += subtotal

            items.append(
                {
                    "item_id": row["item_id"],
                    "name": row["name"],
                    "quantity": quantity,
                    "price": row["price"],
                    "version": row["version"],
                    "subtotal": subtotal,
                }
            )

        if total > 9_000_000_000_000_000_000:
            raise ValidationError("Сумма заявки слишком велика.")

        meta = connection.execute(
            "SELECT revision FROM cart_meta WHERE ticket_id=?",
            (ticket_id,),
        ).fetchone()

        return {
            "items": items,
            "total": total,
            "currency": config["currency"]["resources"],
            "cart_revision": meta["revision"] if meta else 0,
        }

    async def show_catalog(self, interaction, ticket, page=0, query=""):
        order = await self.query_one(
            "SELECT id FROM orders WHERE ticket_id=?",
            (ticket["id"],),
        )

        if order:
            raise ValidationError(
                f"Заявка №{order['id']} уже сохранена. "
                "Посмотреть её можно кнопкой «Корзина / заявка»."
            )

        products = await asyncio.to_thread(
            self.db.list_products,
            "resources",
            None,
            query or None,
        )
        products = [
            product for product in products
            if product["available"]
        ]

        config = await self.config(ticket["guild_id"])
        size = max(1, min(25, config["limits"]["catalog_page_size"]))
        pages = max(1, math.ceil(len(products) / size))
        page = max(0, min(page, pages - 1))
        visible = products[page * size:(page + 1) * size]

        text = [
            f"**Закупка ресурсов — страница {page + 1}/{pages}**",
            f"Цены указаны в {config['currency']['resources']}.",
            "",
        ]

        for product in visible:
            text.append(
                f"• {product['name']} — "
                f"**{number(product['price'])}** за шт."
            )

        if not visible:
            text.append("Подходящих доступных ресурсов нет.")

        text += [
            "",
            "Выбор ресурса задаёт его количество в корзине. "
            "Повторный выбор заменяет количество.",
        ]

        view = CatalogView(
            self,
            interaction.user.id,
            page,
            pages,
            visible,
            query,
        )

        await interaction.followup.send(
            "\n".join(text),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def show_cart(self, interaction, ticket):
        order = await self.query_one(
            "SELECT * FROM orders WHERE ticket_id=?",
            (ticket["id"],),
        )

        if order:
            await send_text(interaction, summary(order))
            return

        rows = await self.query_all(
            """
            SELECT c.item_id, c.quantity_text, p.name,
                   p.price, p.available
            FROM cart_items AS c
            JOIN products AS p
              ON p.catalog=c.catalog AND p.item_id=c.item_id
            WHERE c.ticket_id=? AND c.catalog='resources'
            ORDER BY p.sort_order, p.item_id
            """,
            (ticket["id"],),
        )

        if not rows:
            await reply(
                interaction,
                "Корзина пуста. Нажмите «Каталог».",
            )
            return

        config = await self.config(ticket["guild_id"])
        text = ["**Ваша корзина — текущие цены**", ""]
        total = 0

        for item in rows:
            quantity = int(item["quantity_text"])
            subtotal = quantity * item["price"]
            total += subtotal
            warning = "" if item["available"] else " — НЕ ПРИНИМАЕТСЯ"

            text.append(
                f"• {item['name']}: {number(quantity)} × "
                f"{number(item['price'])} = "
                f"{number(subtotal)}{warning}"
            )

        text += [
            "",
            f"Итого по всем позициям: {number(total)} "
            f"{config['currency']['resources']}.",
            "",
            "Для изменения количества выберите ресурс в каталоге снова. "
            "Недоступные позиции нужно удалить перед подтверждением.",
        ]

        await send_text(interaction, "\n".join(text))

        # В одном меню Discord допускается максимум 25 вариантов.
        for start in range(0, len(rows), 25):
            await interaction.followup.send(
                "Удаление позиции:",
                view=RemoveView(
                    self,
                    interaction.user.id,
                    rows[start:start + 25],
                ),
                ephemeral=True,
            )

    async def show_confirmation(self, interaction, ticket):
        config = await self.config(ticket["guild_id"])

        def prepare(connection):
            self.check_editable(connection, ticket)
            return self.calculation(connection, ticket["id"], config)

        calculation = await self.transaction(prepare)

        text = ["**Проверьте расчёт перед подтверждением**", ""]

        for item in calculation["items"]:
            text.append(
                f"• {item['name']}: {number(item['quantity'])} × "
                f"{number(item['price'])} = "
                f"{number(item['subtotal'])}"
            )

        text += [
            "",
            f"Итого: **{number(calculation['total'])} "
            f"{calculation['currency']}**.",
            "",
            "Оплата производится после получения и проверки ресурсов. "
            "Этот расчёт сам по себе не подтверждает приём или выплату.",
        ]

        await send_text(interaction, "\n".join(text))

        await interaction.followup.send(
            "Подтвердите именно показанный расчёт и укажите игровой ник:",
            view=ConfirmView(
                self,
                interaction.user.id,
                fingerprint(calculation),
            ),
            ephemeral=True,
        )

    def create_order(
        self,
        connection,
        ticket,
        nickname,
        config,
        expected,
    ):
        self.check_editable(connection, ticket)
        calculation = self.calculation(
            connection,
            ticket["id"],
            config,
        )

        if fingerprint(calculation) != expected:
            raise ValidationError(
                "Корзина, цены или условия изменились. "
                "Снова нажмите «Подтвердить заявку» "
                "и проверьте актуальный расчёт."
            )

        now = utc_now()

        cursor = connection.execute(
            """
            INSERT INTO orders(
                ticket_id, guild_id, owner_id, direction,
                game_nickname, status, currency, total,
                snapshot_json, created_at, updated_at
            )
            VALUES(?, ?, ?, 'resources', ?, 'new', ?, ?, ?, ?, ?)
            """,
            (
                ticket["id"],
                ticket["guild_id"],
                ticket["owner_id"],
                nickname,
                calculation["currency"],
                calculation["total"],
                json_dump(calculation),
                now,
                now,
            ),
        )

        order_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO order_events(
                order_id, actor_id, old_status,
                new_status, reason, created_at
            )
            VALUES(?, ?, NULL, 'new', ?, ?)
            """,
            (
                order_id,
                ticket["owner_id"],
                "Пользователь подтвердил состав и расчёт.",
                now,
            ),
        )

        connection.execute(
            """
            UPDATE tickets
            SET game_nickname=?, updated_at=?
            WHERE id=?
            """,
            (nickname, now, ticket["id"]),
        )

        connection.execute(
            """
            INSERT INTO user_profiles(
                guild_id, user_id, game_nickname, updated_at
            )
            VALUES(?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                game_nickname=excluded.game_nickname,
                updated_at=excluded.updated_at
            """,
            (
                ticket["guild_id"],
                ticket["owner_id"],
                nickname,
                now,
            ),
        )

        return order_id

    async def start_mail(self, interaction, ticket):
        async with self.lock(ticket["id"]):
            job = await self.query_one(
                "SELECT * FROM close_jobs WHERE ticket_id=?",
                (ticket["id"],),
            )

            if job and job["kind"] != "mail":
                raise ValidationError(
                    "Для тикета уже начато обычное закрытие. "
                    "Почтовую процедуру запустить нельзя."
                )

            if not job:
                config = await self.config(ticket["guild_id"])
                mail = config["mail"]["resources"]

                if not mail["enabled"] or not mail["recipient"].strip():
                    raise ValidationError(
                        "Почтовый способ сейчас не настроен."
                    )

                mode = config["closing"]["mode"]

                if mode not in {"move", "delete"}:
                    raise ValidationError("Неизвестный режим закрытия.")

                if not config["channels"]["transcripts"]:
                    raise ValidationError("Не настроен канал истории.")

                if mode == "move":
                    category = interaction.guild.get_channel(
                        config["categories"]["closed"]
                    )
                    if not isinstance(category, discord.CategoryChannel):
                        raise ValidationError(
                            "Не настроена категория закрытых тикетов."
                        )

                order = await self.query_one(
                    "SELECT * FROM orders WHERE ticket_id=?",
                    (ticket["id"],),
                )

                if not order:
                    raise ValidationError(
                        "Сначала заполните корзину "
                        "и подтвердите заявку."
                    )

                if order["status"] in {"completed", "cancelled"}:
                    raise ValidationError(
                        "Заявка уже завершена или отменена. "
                        "Обратитесь к сотруднику."
                    )

                contact_url = (
                    f"https://discord.com/channels/"
                    f"{ticket['guild_id']}/"
                    f"{config['channels']['contact']}"
                )

                instructions = self.templates["resources_mail"].format(
                    recipient=mail["recipient"],
                    contact_url=contact_url,
                    summary=summary(order),
                )

                instructions += (
                    "\n\n"
                    + self.templates["mail_capacity"]
                    + "\n\n"
                    + self.templates["order_closed_warning"]
                )

                parts = list(split_text(instructions, 1800))
                now = utc_now()

                def prepare(connection):
                    current = connection.execute(
                        "SELECT state FROM tickets WHERE id=?",
                        (ticket["id"],),
                    ).fetchone()

                    current_order = connection.execute(
                        "SELECT * FROM orders WHERE id=?",
                        (order["id"],),
                    ).fetchone()

                    if not current or current["state"] != "open":
                        raise ValidationError(
                            "Состояние тикета изменилось."
                        )

                    if (
                        not current_order
                        or current_order["version"] != order["version"]
                    ):
                        raise ValidationError(
                            "Заявка изменилась. Повторите выбор почты."
                        )

                    connection.execute(
                        """
                        INSERT INTO close_jobs(
                            ticket_id, kind, state, reason,
                            actor_id, snapshot_json,
                            mail_parts_json, close_mode,
                            created_at, updated_at
                        )
                        VALUES(
                            ?, 'mail', 'mail_pending', ?, ?, ?,
                            ?, ?, ?, ?
                        )
                        """,
                        (
                            ticket["id"],
                            "Пользователь выбрал игровую почту.",
                            interaction.user.id,
                            json_dump(
                                {
                                    "ticket": ticket,
                                    "order": order,
                                    "recipient": mail["recipient"],
                                }
                            ),
                            json_dump(parts),
                            mode,
                            now,
                            now,
                        ),
                    )

                    connection.execute(
                        """
                        UPDATE tickets
                        SET state='closing', updated_at=?
                        WHERE id=?
                        """,
                        (now, ticket["id"]),
                    )

                await self.transaction(prepare)

            try:
                await self.continue_close(ticket["id"])
            except discord.Forbidden:
                await reply(
                    interaction,
                    self.templates["dm_unavailable"],
                )
                return
            except Exception:
                log.exception(
                    "Не завершена почтовая процедура тикета %s",
                    ticket["id"],
                )
                await reply(
                    interaction,
                    "Процедура пока не завершена. "
                    "Доставленные части инструкций учтены в базе. "
                    "После устранения ошибки нажмите "
                    "«Почта / повторить отправку».\n"
                    "Подробности ошибки — в терминале.",
                )
                return

        try:
            await reply(
                interaction,
                "Инструкции отправлены в личные сообщения. "
                "История сохранена, тикет закрыт. "
                "Заявка остаётся доступна сотрудникам.",
            )
        except discord.HTTPException:
            pass

    async def continue_close(self, ticket_id):
        job = await self.query_one(
            "SELECT * FROM close_jobs WHERE ticket_id=?",
            (ticket_id,),
        )

        if job and job["kind"] == "mail" and job["state"] == "mail_pending":
            ticket = await self.query_one(
                "SELECT * FROM tickets WHERE id=?",
                (ticket_id,),
            )

            user = self.client.get_user(ticket["owner_id"])
            if user is None:
                user = await self.client.fetch_user(ticket["owner_id"])

            dm = await user.create_dm()
            parts = json.loads(job["mail_parts_json"])

            # Восстановление после сбоя между отправкой в Discord
            # и записью результата в SQLite.
            delivered_markers = {}

            async for message in dm.history(limit=200):
                if message.author.id != self.client.user.id:
                    continue

                for embed in message.embeds:
                    marker = embed.footer.text
                    if marker:
                        delivered_markers[marker] = message.id

            for index, content in enumerate(parts):
                saved = await self.query_one(
                    """
                    SELECT * FROM dm_parts
                    WHERE ticket_id=? AND part_index=?
                    """,
                    (ticket_id, index),
                )

                if saved and saved["state"] == "sent":
                    continue

                marker = f"StalPoint mail:{ticket_id}:{index}"
                message_id = delivered_markers.get(marker)

                if message_id is None:
                    embed = discord.Embed(
                        description=(
                            f"Инструкции к тикету №{ticket_id}. "
                            f"Часть {index + 1}/{len(parts)}."
                        ),
                        color=discord.Color.blue(),
                    )
                    embed.set_footer(text=marker)

                    message = await dm.send(
                        content,
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    message_id = message.id

                await self.execute(
                    """
                    INSERT INTO dm_parts(
                        ticket_id, part_index, content_sha256,
                        state, message_id, sent_at
                    )
                    VALUES(?, ?, ?, 'sent', ?, ?)
                    ON CONFLICT(ticket_id, part_index) DO UPDATE SET
                        state='sent',
                        message_id=excluded.message_id,
                        sent_at=excluded.sent_at,
                        last_error=NULL
                    """,
                    (
                        ticket_id,
                        index,
                        hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest(),
                        message_id,
                        utc_now(),
                    ),
                )

            await self.execute(
                """
                UPDATE close_jobs
                SET state='prepared',
                    instructions_complete_at=?,
                    updated_at=?, last_error=NULL
                WHERE ticket_id=? AND state='mail_pending'
                """,
                (utc_now(), utc_now(), ticket_id),
            )

        # Штатная процедура сначала сохраняет и отправляет историю,
        # только затем переносит или удаляет канал.
        await super().continue_close(ticket_id)

    async def refresh_resource_interface(self, ticket):
        if not ticket["interface_message_id"]:
            return

        channel = await self.channel(ticket["channel_id"])
        message = await channel.fetch_message(
            ticket["interface_message_id"]
        )
        config = await self.config(ticket["guild_id"])

        description = self.templates["resources_intro"].format(
            recipient=config["mail"]["resources"]["recipient"],
        )

        description += (
            "\n\n**Как оформить продажу:**\n"
            "1. Откройте каталог и задайте количество ресурсов.\n"
            "2. Проверьте корзину и подтвердите заявку.\n"
            "3. Нажмите «Почта / повторить отправку».\n\n"
            "Инструкции поступят в личные сообщения. "
            "После их доставки и сохранения истории тикет закроется.\n"
            "Получение ресурсов и выплату подтверждает сотрудник."
        )

        embed = discord.Embed(
            title=f"Тикет №{ticket['id']} • Продать ресурсы",
            description=description,
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"StalPoint • ticket:{ticket['id']}")

        await message.edit(
            embed=embed,
            view=ResourceControls(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def finish_creation(self, ticket, channel):
        await super().finish_creation(ticket, channel)

        if ticket["direction"] == "resources":
            current = await self.query_one(
                "SELECT * FROM tickets WHERE id=?",
                (ticket["id"],),
            )
            await self.refresh_resource_interface(current)

    async def publish_panels(self, guild):
        await super().publish_panels(guild)

        panel = await self.query_one(
            "SELECT * FROM panels WHERE panel_key=?",
            (f"{guild.id}:resources",),
        )

        if panel:
            channel = await self.channel(panel["channel_id"])
            message = await channel.fetch_message(panel["message_id"])
            embed = message.embeds[0]

            embed.description = (
                "Создайте приватный тикет, выберите ресурсы "
                "и количество, затем проверьте расчёт.\n\n"
                "Основной способ передачи — игровая почта. "
                "Оплата производится после получения и проверки."
            )

            await message.edit(embed=embed)

    async def worker(self):
        await self.client.wait_until_ready()

        tickets = await self.query_all(
            """
            SELECT * FROM tickets
            WHERE direction='resources' AND state='open'
            """
        )

        for ticket in tickets:
            try:
                await self.refresh_resource_interface(ticket)
            except Exception:
                log.exception(
                    "Не обновлён интерфейс ресурсов %s",
                    ticket["id"],
                )

        await super().worker()

    async def require_resource_staff(self, interaction):
        if interaction.guild is None:
            raise ValidationError("Команда доступна только на сервере.")

        allowed = await self.is_staff(
            interaction.user,
            {
                "guild_id": interaction.guild.id,
                "direction": "resources",
            },
        )

        if not allowed:
            raise ValidationError(
                "Нужна роль сотрудника направления «Ресурсы» "
                "или права администратора."
            )

    def register(self):
        super().register()
        self.client.add_view(ResourceControls(self))

        @self.client.tree.command(
            name="resource_orders",
            description="Заявки на ресурсы: список или просмотр по номеру",
        )
        @app_commands.guild_only()
        @app_commands.describe(
            order_id="Номер заявки; 0 — список активных",
            page="Страница списка активных заявок",
        )
        async def resource_orders(
            interaction: discord.Interaction,
            order_id: int = 0,
            page: int = 1,
        ):
            await interaction.response.defer(ephemeral=True)

            try:
                await self.require_resource_staff(interaction)

                if order_id:
                    order = await self.query_one(
                        """
                        SELECT * FROM orders
                        WHERE id=? AND guild_id=?
                          AND direction='resources'
                        """,
                        (order_id, interaction.guild_id),
                    )

                    if not order:
                        raise ValidationError("Заявка не найдена.")

                    await send_text(interaction, summary(order))

                    events = await self.query_all(
                        """
                        SELECT * FROM order_events
                        WHERE order_id=?
                        ORDER BY id DESC LIMIT 10
                        """,
                        (order_id,),
                    )

                    history = ["**Последние действия:**"]

                    for event in reversed(events):
                        history.append(
                            f"• {event['created_at']} | "
                            f"сотрудник/пользователь {event['actor_id']} | "
                            f"{event['new_status']} | "
                            f"{event['reason'] or 'Без пояснения'}"
                        )

                    await send_text(interaction, "\n".join(history))

                    await interaction.followup.send(
                        "Изменение статуса фиксирует действие сотрудника. "
                        "Бот не проверяет игровую почту и переводы.",
                        view=StaffOrderView(self, order),
                        ephemeral=True,
                    )
                    return

                count = await self.query_one(
                    """
                    SELECT COUNT(*) AS amount FROM orders
                    WHERE guild_id=? AND direction='resources'
                      AND status IN ('new', 'in_progress')
                    """,
                    (interaction.guild_id,),
                )

                pages = max(1, math.ceil(count["amount"] / 10))
                page = max(1, min(page, pages))

                orders = await self.query_all(
                    """
                    SELECT * FROM orders
                    WHERE guild_id=? AND direction='resources'
                      AND status IN ('new', 'in_progress')
                    ORDER BY id DESC
                    LIMIT 10 OFFSET ?
                    """,
                    (interaction.guild_id, (page - 1) * 10),
                )

                lines = [
                    f"**Активные заявки — {page}/{pages}**",
                    "",
                ]

                for order in orders:
                    lines.append(
                        f"№{order['id']} — "
                        f"{order['game_nickname']} — "
                        f"{number(order['total'])} "
                        f"{order['currency']} — "
                        f"{STATUSES[order['status']]}"
                    )

                if not orders:
                    lines.append("Активных заявок нет.")

                lines += [
                    "",
                    "Просмотр: /resource_orders order_id:НОМЕР",
                    "Другая страница: /resource_orders page:НОМЕР",
                    "Завершённые и отменённые заявки также "
                    "можно открыть по номеру.",
                ]

                await send_text(interaction, "\n".join(lines))

            except ValidationError as error:
                await reply(interaction, str(error))