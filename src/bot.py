import csv
import io
import re
from datetime import datetime, timezone as dt_timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from src.database import Database
from src.settings import settings
from src.utils import parse_timezone

HELP_TEXT = """Health Tracker Bot

/add <metric> [HH:MM] — add metric with optional daily reminder
  /add mood 09:00
  /add stool
/list — list your metrics
/track <metric> [0-5] — record a value
  /track mood 4
  /track stool
/delete <metric> — remove metric and all its data
/export [metric] — download CSV
/timezone ±HH:MM — set your timezone"""


class TrackStates(StatesGroup):
    waiting_for_value = State()


def _build_value_keyboard(metric_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"record:{metric_id}:{i}")
        for i in range(6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


class HealthBot:
    def __init__(self, db: Database):
        self.db = db
        bot_kwargs = {"token": settings.telegram_bot_token}
        if settings.telegram_api_server:
            bot_kwargs["base_url"] = settings.telegram_api_server
        self.bot = Bot(**bot_kwargs)
        self.dp = Dispatcher(storage=MemoryStorage())

        router = Router()

        router.message.register(self._cmd_start, Command("start"))
        router.message.register(self._cmd_add, Command("add"))
        router.message.register(self._cmd_list, Command("list"))
        router.message.register(self._cmd_delete, Command("delete"))
        router.message.register(self._cmd_track, Command("track"))
        router.message.register(self._cmd_export, Command("export"))
        router.message.register(self._cmd_timezone, Command("timezone"))
        router.message.register(self._fsm_value_input, TrackStates.waiting_for_value)
        router.callback_query.register(self._cb_record, F.data.startswith("record:"))

        self.dp.include_router(router)

    async def start(self):
        await self.dp.start_polling(self.bot)

    async def send_reminder(self, user_id: int, metric: dict) -> None:
        text = f"How is your {metric['name']}? (0 — bad, 5 — great)"
        keyboard = _build_value_keyboard(metric["id"])
        await self.bot.send_message(user_id, text, reply_markup=keyboard)

    async def _cmd_start(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            await self.db.get_or_create_user(user_id, settings.default_timezone)
            logger.info("User {} started bot", user_id)
            await message.answer(HELP_TEXT)
        except Exception as e:
            logger.warning("Error in /start: {}", e)
            await message.answer("Error occurred.")

    async def _cmd_add(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            args = message.text.split()[1:]
            if not args:
                await message.answer("Usage: /add <metric> [HH:MM]")
                return
            name = args[0]
            remind_time = None
            if len(args) >= 2 and re.match(r'^\d{2}:\d{2}$', args[1]):
                try:
                    datetime.strptime(args[1], "%H:%M")
                    remind_time = args[1]
                except ValueError:
                    await message.answer("Invalid time. Use HH:MM format (e.g. 09:00)")
                    return
            await self.db.get_or_create_user(user_id, settings.default_timezone)
            result = await self.db.add_metric(user_id, name, remind_time)
            if result is None:
                await message.answer(f'Metric "{name}" already exists')
                return
            if remind_time:
                await message.answer(f"Added: {name}, reminder at {remind_time}")
            else:
                await message.answer(f"Added: {name} (no reminder)")
        except Exception as e:
            logger.warning("Error in /add: {}", e)
            await message.answer("Error occurred.")

    async def _cmd_list(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            metrics = await self.db.get_metrics(user_id)
            if not metrics:
                await message.answer("No metrics yet. Use /add")
                return
            lines = ["Your metrics:"]
            for m in metrics:
                if m["remind_time"]:
                    lines.append(f"• {m['name']} (reminder at {m['remind_time']})")
                else:
                    lines.append(f"• {m['name']} (no reminder)")
            await message.answer("\n".join(lines))
        except Exception as e:
            logger.warning("Error in /list: {}", e)
            await message.answer("Error occurred.")

    async def _cmd_delete(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            args = message.text.split()[1:]
            if not args:
                await message.answer("Usage: /delete <metric>")
                return
            name = args[0]
            deleted = await self.db.delete_metric(user_id, name)
            if deleted:
                await message.answer(f'Metric "{name}" deleted')
            else:
                await message.answer(f'Metric "{name}" not found')
        except Exception as e:
            logger.warning("Error in /delete: {}", e)
            await message.answer("Error occurred.")

    async def _cmd_track(self, message: Message, state: FSMContext) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            args = message.text.split()[1:]
            if not args:
                await message.answer("Usage: /track <metric> [0-5]")
                return
            name = args[0]
            metric = await self.db.get_metric_by_name(user_id, name)
            if metric is None:
                await message.answer(f'Metric "{name}" not found')
                return
            if len(args) >= 2:
                try:
                    value = int(args[1])
                except ValueError:
                    await message.answer("Value must be a number from 0 to 5")
                    return
                if value < 0 or value > 5:
                    await message.answer("Value must be between 0 and 5")
                    return
                await self.db.add_record(user_id, metric["id"], value)
                await message.answer(f"✅ {name}: {value}")
            else:
                await state.set_data({"metric_id": metric["id"], "metric_name": metric["name"]})
                await state.set_state(TrackStates.waiting_for_value)
                keyboard = _build_value_keyboard(metric["id"])
                await message.answer(
                    f"How is your {metric['name']}? (0 — bad, 5 — great)",
                    reply_markup=keyboard,
                )
        except Exception as e:
            logger.warning("Error in /track: {}", e)
            await message.answer("Error occurred.")

    async def _fsm_value_input(self, message: Message, state: FSMContext) -> None:
        try:
            if message.from_user is None:
                return
            text = message.text.strip()
            if text in {"0", "1", "2", "3", "4", "5"}:
                value = int(text)
                data = await state.get_data()
                metric_id = data["metric_id"]
                metric_name = data["metric_name"]
                user_id = message.from_user.id
                await self.db.add_record(user_id, metric_id, value)
                await state.clear()
                await message.answer(f"✅ {metric_name}: {value}")
            else:
                await message.answer("Please send a number from 0 to 5")
        except Exception as e:
            logger.warning("Error in FSM value input: {}", e)
            await message.answer("Error occurred.")

    async def _cmd_export(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            args = message.text.split()[1:]
            metric_name = args[0] if args else None
            user = await self.db.get_user(user_id)
            tz_str = user["timezone"] if user else "+00:00"
            records = await self.db.get_records(user_id, metric_name)
            if not records:
                await message.answer("No data to export")
                return

            user_tz = parse_timezone(tz_str)

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["metric", "value", "recorded_at_utc", "recorded_at_local"])
            for r in records:
                ts = r["recorded_at"]
                dt_utc = datetime.fromtimestamp(ts, tz=dt_timezone.utc)
                dt_local = dt_utc.astimezone(user_tz)
                utc_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                local_str = dt_local.isoformat(timespec='seconds')
                writer.writerow([r["metric_name"], r["value"], utc_str, local_str])

            csv_bytes = buf.getvalue().encode("utf-8")
            await self.bot.send_document(
                user_id,
                BufferedInputFile(csv_bytes, filename="health_export.csv"),
            )
        except Exception as e:
            logger.warning("Error in /export: {}", e)
            await message.answer("Error occurred.")

    async def _cmd_timezone(self, message: Message) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            args = message.text.split()[1:]
            if not args or not re.match(r'^[+-]\d{2}:\d{2}$', args[0]):
                await message.answer("Usage: /timezone ±HH:MM (e.g. +03:00)")
                return
            tz_str = args[0]
            await self.db.get_or_create_user(user_id, settings.default_timezone)
            await self.db.update_user_timezone(user_id, tz_str)
            await message.answer(f"Timezone set: {tz_str}")
        except Exception as e:
            logger.warning("Error in /timezone: {}", e)
            await message.answer("Error occurred.")

    async def _cb_record(self, callback: CallbackQuery, state: FSMContext) -> None:
        try:
            if callback.from_user is None:
                await callback.answer()
                return
            parts = callback.data.split(":")
            metric_id = int(parts[1])
            value = int(parts[2])
            user_id = callback.from_user.id
            metric = await self.db.get_metric_by_id(metric_id)
            if metric is None or metric["user_id"] != user_id:
                await callback.answer("Not your metric")
                return
            await self.db.get_or_create_user(user_id, settings.default_timezone)
            await self.db.add_record(user_id, metric_id, value)
            metric_name = metric["name"]
            await callback.message.edit_text(f"✅ {metric_name}: {value}", reply_markup=None)
            await callback.answer()
            current_state = await state.get_state()
            if current_state == TrackStates.waiting_for_value:
                await state.clear()
        except Exception as e:
            logger.warning("Error in callback record: {}", e)
            await callback.answer("Error")
