"""
Telegram-бот для отслеживания записей на визу из Google Sheets.

Команды:
  /start          — Приветствие и список команд
  /all             — Все группы туристов
  /manager <КОД>   — Туристы конкретного менеджера (AN, DB, SB)
  /nodate          — Туристы БЕЗ подтверждённой даты записи
  /nodate <КОД>    — Без даты у конкретного менеджера
  /recorded        — Туристы С подтверждённой датой записи
  /recorded <КОД>  — С датой у конкретного менеджера
  /stats           — Общая статистика
  /refresh         — Принудительно обновить данные из таблицы
"""

import logging
import csv
import io
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

import httpx
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

import os

# Токен берётся из переменной окружения (безопасно для хостинга)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ID таблицы из ссылки Google Sheets (можно переопределить через переменную окружения)
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1GfSqn_RS6hX-CBxJFgTEmjENPLM_chizfQyAZ_LiK4g")
SHEET_GID = "0"

# Кэш: данные обновляются не чаще раза в 5 минут
CACHE_TTL_SECONDS = 300

# ─── ЛОГИРОВАНИЕ ──────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── МОДЕЛИ ДАННЫХ ────────────────────────────────────────────────────────────

@dataclass
class Tourist:
    """Один турист (строка таблицы)."""
    last_name: str
    first_name: str
    birth_date: str
    passport: str
    valid_until: str
    phone: str
    appointment_slots: str   # Колонка G — доступные слоты
    confirmed_date: str      # Колонка H — подтверждённая дата
    sign: str                # Колонка I — признак (ТУР)
    operator: str            # Колонка J — оператор
    departure: str           # Колонка K — дата вылета
    manager: str             # Колонка L — менеджер (AN, DB, SB)

    @property
    def full_name(self) -> str:
        return f"{self.last_name} {self.first_name}"

    @property
    def has_confirmed_date(self) -> bool:
        return bool(self.confirmed_date.strip())

    @property
    def has_appointment_slots(self) -> bool:
        return bool(self.appointment_slots.strip())


@dataclass
class TouristGroup:
    """Группа туристов (семья/компания), разделённая пустыми строками."""
    lead: Tourist
    members: list[Tourist] = field(default_factory=list)

    @property
    def manager(self) -> str:
        return self.lead.manager.strip().upper()

    @property
    def has_confirmed_date(self) -> bool:
        return self.lead.has_confirmed_date

    @property
    def confirmed_date(self) -> str:
        return self.lead.confirmed_date

    @property
    def appointment_slots(self) -> str:
        return self.lead.appointment_slots

    @property
    def operator(self) -> str:
        return self.lead.operator

    @property
    def departure(self) -> str:
        return self.lead.departure

    @property
    def all_members(self) -> list[Tourist]:
        return [self.lead] + self.members

    @property
    def names_str(self) -> str:
        names = [m.full_name for m in self.all_members]
        return ", ".join(names)

    @property
    def member_count(self) -> int:
        return len(self.all_members)


# ─── ЗАГРУЗКА ДАННЫХ ──────────────────────────────────────────────────────────

class SheetData:
    """Загрузка и кэширование данных из Google Sheets."""

    def __init__(self):
        self.groups: list[TouristGroup] = []
        self._last_fetched: Optional[datetime] = None

    @property
    def _cache_valid(self) -> bool:
        if self._last_fetched is None:
            return False
        return (datetime.now() - self._last_fetched).total_seconds() < CACHE_TTL_SECONDS

    async def get_groups(self, force_refresh: bool = False) -> list[TouristGroup]:
        if not force_refresh and self._cache_valid and self.groups:
            return self.groups
        await self._fetch()
        return self.groups

    async def _fetch(self):
        url = (
            f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
            f"/export?format=csv&gid={SHEET_GID}"
        )
        logger.info("Загрузка таблицы: %s", url)

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        text = resp.text
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)

        if not rows:
            logger.warning("Таблица пуста")
            return

        # Пропускаем заголовок (строка 1)
        header = rows[0]
        data_rows = rows[1:]

        self.groups = self._parse_groups(data_rows)
        self._last_fetched = datetime.now()
        logger.info("Загружено %d групп, %d туристов",
                     len(self.groups),
                     sum(g.member_count for g in self.groups))

    @staticmethod
    def _row_to_tourist(row: list[str]) -> Optional[Tourist]:
        """Преобразует строку CSV в объект Tourist."""
        # Дополняем короткие строки пустыми значениями
        while len(row) < 12:
            row.append("")

        last_name = row[0].strip()
        first_name = row[1].strip()

        if not last_name and not first_name:
            return None

        return Tourist(
            last_name=last_name,
            first_name=first_name,
            birth_date=row[2].strip(),
            passport=row[3].strip(),
            valid_until=row[4].strip(),
            phone=row[5].strip(),
            appointment_slots=row[6].strip(),
            confirmed_date=row[7].strip(),
            sign=row[8].strip(),
            operator=row[9].strip(),
            departure=row[10].strip(),
            manager=row[11].strip(),
        )

    @staticmethod
    def _parse_groups(rows: list[list[str]]) -> list[TouristGroup]:
        """Парсит строки в группы, разделённые пустыми строками."""
        groups: list[TouristGroup] = []
        current_group: Optional[TouristGroup] = None

        for row in rows:
            tourist = SheetData._row_to_tourist(row)

            if tourist is None:
                # Пустая строка — конец группы
                if current_group is not None:
                    groups.append(current_group)
                    current_group = None
                continue

            if current_group is None:
                # Новая группа: первый турист — лид
                current_group = TouristGroup(lead=tourist)
            else:
                # Дополнительный участник группы
                current_group.members.append(tourist)

        # Не забываем последнюю группу
        if current_group is not None:
            groups.append(current_group)

        return groups


# Глобальный экземпляр
sheet_data = SheetData()


# ─── ФОРМАТИРОВАНИЕ ───────────────────────────────────────────────────────────

def format_group(g: TouristGroup, index: int) -> str:
    """Форматирует одну группу для Telegram-сообщения."""
    lines = []
    # Заголовок группы
    status = "✅" if g.has_confirmed_date else "❌"
    lines.append(f"{status} <b>{index}. {g.lead.full_name}</b>")

    # Участники
    if g.members:
        member_names = [f"  👤 {m.full_name}" for m in g.members]
        lines.extend(member_names)

    # Детали
    details = []
    if g.operator:
        details.append(f"🏢 {g.operator}")
    if g.departure:
        details.append(f"✈️ Вылет: {g.departure}")
    if g.manager:
        details.append(f"👔 Менеджер: {g.manager}")

    if g.appointment_slots:
        details.append(f"📅 Слоты: {g.appointment_slots}")

    if g.has_confirmed_date:
        details.append(f"🎯 Записаны: {g.confirmed_date}")
    else:
        details.append("⚠️ <b>Дата НЕ подтверждена</b>")

    lines.append("  " + " | ".join(details))
    return "\n".join(lines)


def format_groups(groups: list[TouristGroup], title: str) -> list[str]:
    """Форматирует список групп, разбивая на сообщения по 4096 символов."""
    if not groups:
        return [f"📋 <b>{title}</b>\n\nНичего не найдено."]

    total_tourists = sum(g.member_count for g in groups)
    header = f"📋 <b>{title}</b>\n👥 Групп: {len(groups)} | Туристов: {total_tourists}\n"
    separator = "\n" + "─" * 30 + "\n"

    messages = []
    current = header

    for i, g in enumerate(groups, 1):
        block = separator + format_group(g, i)

        if len(current) + len(block) > 3900:
            messages.append(current)
            current = f"📋 <b>{title}</b> (продолжение)\n" + block
        else:
            current += block

    if current:
        messages.append(current)

    return messages


async def send_messages(update: Update, messages: list[str]):
    """Отправляет список сообщений в чат."""
    for msg in messages:
        await update.message.reply_text(msg, parse_mode="HTML")


# ─── КОМАНДЫ БОТА ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Бот отслеживания записей на визу</b>\n\n"
        "📊 Данные загружаются из Google Sheets\n\n"
        "<b>Команды:</b>\n\n"
        "/all — Все группы туристов\n"
        "/manager AN — Туристы менеджера (AN, DB, SB)\n"
        "/nodate — Кто ещё БЕЗ записи\n"
        "/nodate AN — Без записи у менеджера\n"
        "/recorded — Кто уже записан\n"
        "/recorded AN — Записанные у менеджера\n"
        "/stats — Общая статистика\n"
        "/refresh — Обновить данные\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = await sheet_data.get_groups()
    messages = format_groups(groups, "Все туристы")
    await send_messages(update, messages)


async def cmd_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        managers = set()
        groups = await sheet_data.get_groups()
        for g in groups:
            if g.manager:
                managers.add(g.manager)

        text = (
            "👔 <b>Укажите код менеджера:</b>\n\n"
            f"/manager <код>\n\n"
            f"Доступные менеджеры: {', '.join(sorted(managers))}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        return

    code = context.args[0].strip().upper()
    groups = await sheet_data.get_groups()
    filtered = [g for g in groups if g.manager == code]
    messages = format_groups(filtered, f"Туристы менеджера {code}")
    await send_messages(update, messages)


async def cmd_nodate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = await sheet_data.get_groups()

    manager_filter = None
    if context.args:
        manager_filter = context.args[0].strip().upper()

    filtered = [g for g in groups if not g.has_confirmed_date]
    if manager_filter:
        filtered = [g for g in filtered if g.manager == manager_filter]

    title = "Без подтверждённой записи"
    if manager_filter:
        title += f" (менеджер {manager_filter})"

    messages = format_groups(filtered, title)
    await send_messages(update, messages)


async def cmd_recorded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = await sheet_data.get_groups()

    manager_filter = None
    if context.args:
        manager_filter = context.args[0].strip().upper()

    filtered = [g for g in groups if g.has_confirmed_date]
    if manager_filter:
        filtered = [g for g in filtered if g.manager == manager_filter]

    title = "С подтверждённой записью"
    if manager_filter:
        title += f" (менеджер {manager_filter})"

    messages = format_groups(filtered, title)
    await send_messages(update, messages)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = await sheet_data.get_groups()

    total_groups = len(groups)
    total_tourists = sum(g.member_count for g in groups)
    with_date = [g for g in groups if g.has_confirmed_date]
    without_date = [g for g in groups if not g.has_confirmed_date]

    # По менеджерам
    manager_stats = {}
    for g in groups:
        m = g.manager or "—"
        if m not in manager_stats:
            manager_stats[m] = {"total": 0, "recorded": 0, "pending": 0, "tourists": 0}
        manager_stats[m]["total"] += 1
        manager_stats[m]["tourists"] += g.member_count
        if g.has_confirmed_date:
            manager_stats[m]["recorded"] += 1
        else:
            manager_stats[m]["pending"] += 1

    # По операторам
    operator_stats = {}
    for g in groups:
        op = g.operator or "—"
        operator_stats[op] = operator_stats.get(op, 0) + 1

    text = (
        "📊 <b>Статистика записей на визу</b>\n\n"
        f"👥 Всего групп: <b>{total_groups}</b>\n"
        f"🧑 Всего туристов: <b>{total_tourists}</b>\n"
        f"✅ Записаны: <b>{len(with_date)}</b> групп\n"
        f"❌ Не записаны: <b>{len(without_date)}</b> групп\n\n"
    )

    text += "─" * 30 + "\n"
    text += "👔 <b>По менеджерам:</b>\n\n"
    for m in sorted(manager_stats.keys()):
        s = manager_stats[m]
        text += (
            f"  <b>{m}</b>: {s['total']} групп ({s['tourists']} чел.) — "
            f"✅ {s['recorded']} | ❌ {s['pending']}\n"
        )

    text += "\n" + "─" * 30 + "\n"
    text += "🏢 <b>По операторам:</b>\n\n"
    for op in sorted(operator_stats.keys()):
        text += f"  {op}: {operator_stats[op]} групп\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Обновляю данные из таблицы...")
    try:
        groups = await sheet_data.get_groups(force_refresh=True)
        total = sum(g.member_count for g in groups)
        await update.message.reply_text(
            f"✅ Обновлено! Загружено {len(groups)} групп, {total} туристов.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка обновления: %s", e)
        await update.message.reply_text(
            f"❌ Ошибка загрузки: {e}\n\n"
            "Проверьте, что таблица открыта для просмотра по ссылке.",
        )


async def post_init(app: Application):
    """Устанавливает команды бота в меню Telegram."""
    commands = [
        BotCommand("start", "Приветствие и список команд"),
        BotCommand("all", "Все туристы"),
        BotCommand("manager", "По менеджеру (AN, DB, SB)"),
        BotCommand("nodate", "Без подтверждённой записи"),
        BotCommand("recorded", "С подтверждённой записью"),
        BotCommand("stats", "Общая статистика"),
        BotCommand("refresh", "Обновить данные"),
    ]
    await app.bot.set_my_commands(commands)


# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("⚠️  Укажите токен бота в переменной окружения BOT_TOKEN!")
        print("   Получите его у @BotFather в Telegram.")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("manager", cmd_manager))
    app.add_handler(CommandHandler("nodate", cmd_nodate))
    app.add_handler(CommandHandler("recorded", cmd_recorded))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("refresh", cmd_refresh))

    print("🤖 Бот запущен! Нажмите Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
