import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.database import Database


def main():
    config_path = ROOT / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    database_path = ROOT / config["database_path"]
    database = Database(database_path)

    database.migrate()

    imported = database.initial_import(
        data_directory=ROOT / "data",
        max_price=config["limits"]["max_price"],
        seller_status=config["arsenal"]["initial_seller_status"],
    )

    exports_directory = ROOT / config["exports_directory"]
    exports_directory.mkdir(parents=True, exist_ok=True)

    print(f"SQLite: {database_path}")

    if imported:
        for catalog, count in imported.items():
            print(f"Импортировано: {catalog} — {count} позиций")
    else:
        print("Первичный импорт уже выполнен.")
        print("Цены и наличие в SQLite сохранены без изменений.")

    expected = {
        "resources": 113,
        "boosts": 27,
        "arsenal": 10,
    }

    print("\nТекущий состав базы:")

    for catalog, initial_count in expected.items():
        row = database.fetchone(
            "SELECT COUNT(*) AS amount FROM products WHERE catalog=?",
            (catalog,),
        )

        amount = row["amount"]
        print(
            f"  {catalog}: {amount} "
            f"(в исходном прайсе: {initial_count})"
        )

    faq = database.fetchall(
        "SELECT topic_id, label FROM faq_topics ORDER BY sort_order"
    )

    print("\nТемы обращений:")
    for topic in faq:
        print(f"  {topic['topic_id']}: {topic['label']}")

    print("\nИнициализация завершена.")
    print("Discord-бот этим скриптом не запускается.")


if __name__ == "__main__":
    main()