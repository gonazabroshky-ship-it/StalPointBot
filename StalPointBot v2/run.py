import asyncio
import logging

import discord

import main
from bot.resource_system import ResourceSystem as TicketSystem


logger = logging.getLogger("stalpoint.run")

service = TicketSystem(
    client=main.client,
    database=main.database,
    root=main.ROOT,
    read_config=main.read_server_config,
)

original_setup_hook = main.client.setup_hook


async def setup_hook():
    # Регистрируем постоянные кнопки и меню во время запуска,
    # до синхронизации команд.
    service.register()

    await original_setup_hook()

    service.worker_task = asyncio.create_task(
        service.worker(),
        name="stalpoint-recovery",
    )

    logger.info(
        "Обработчики панелей зарегистрированы. "
        "Чтение содержимого сообщений: %s",
        main.client.intents.message_content,
    )


main.client.setup_hook = setup_hook


@main.client.event
async def on_interaction(interaction):
    data = interaction.data or {}

    logger.info(
        "ВЗАИМОДЕЙСТВИЕ | тип=%s | канал=%s | элемент=%s",
        interaction.type,
        interaction.channel_id,
        data.get("custom_id") or data.get("name"),
    )


@main.client.event
async def on_message(message):
    if not message.author.bot and message.guild is not None:
        logger.info(
            "СООБЩЕНИЕ | канал=%s | автор=%s | "
            "символов=%s | вложений=%s",
            message.channel.id,
            message.author.id,
            len(message.content),
            len(message.attachments),
        )

    try:
        await service.handle_message(message)
    except Exception:
        logger.exception(
            "Ошибка обработки сообщения %s",
            message.id,
        )


@main.client.event
async def on_disconnect():
    logger.warning("Соединение с Discord прервано.")


@main.client.event
async def on_resumed():
    logger.info("Соединение с Discord восстановлено.")


if __name__ == "__main__":
    main.client.run(main.TOKEN, log_handler=None)