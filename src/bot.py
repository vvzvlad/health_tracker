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
    BotCommand,
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

/add — add a new metric (with optional daily reminder)
/list — list your metrics
/track — record a value for a metric
/delete <metric> — remove metric and all its data
/export [metric] — download CSV
/timezone ±HH:MM — set your timezone"""


class AddStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_time = State()


class TrackStates(StatesGroup):
    waiting_for_metric = State()
    waiting_for_value = State()


def _build_value_keyboard(metric_id: int) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton(text=str(i), callback_data=f"record:{metric_id}:{i}")
        for i in range(-5, 0)
    ]
    row2 = [
        InlineKeyboardButton(text=str(i), callback_data=f"record:{metric_id}:{i}")
        for i in range(0, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])


def _format_history(records: list[dict]) -> str:
    if not records:
        return "no history"
    return " ".join(f"{r['value']:+d}" for r in records)


def _build_metrics_keyboard(metrics: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=m["name"], callback_data=f"pick_metric:{m['id']}:{m['name']}")]
        for m in metrics
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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

        router.message.register(self._fsm_add_name, AddStates.waiting_for_name)
        router.message.register(self._fsm_add_time, AddStates.waiting_for_time)
        router.message.register(self._fsm_track_metric, TrackStates.waiting_for_metric)
        router.message.register(self._fsm_value_input, TrackStates.waiting_for_value)

        router.callback_query.register(self._cb_pick_metric, F.data.startswith("pick_metric:"))
        router.callback_query.register(self._cb_record, F.data.startswith("record:"))

        self.dp.include_router(router)

    async def start(self):
        await self.bot.set_my_commands([
            BotCommand(command="start", description="Show help"),
            BotCommand(command="add", description="Add a new metric"),
            BotCommand(command="list", description="List your metrics"),
            BotCommand(command="track", description="Record a value for a metric"),
            BotCommand(command="delete", description="Remove metric and all its data"),
            BotCommand(command="export", description="Download CSV export"),
            BotCommand(command="timezone", description="Set your timezone (±HH:MM)"),
        ])
        await self.dp.start_polling(self.bot)

    async def send_reminder(self, user_id: int, metric: dict) -> None:
        history = await self.db.get_last_records(metric["id"])
        history_str = _format_history(history)
        text = f"How is your {metric['name']}? (-5 — very bad, +5 — great)\nLast 10: {history_str}"
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

    async def _cmd_add(self, message: Message, state: FSMContext) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            await self.db.get_or_create_user(user_id, settings.default_timezone)
            await state.set_state(AddStates.waiting_for_name)
            await message.answer("Enter the metric name:")
        except Exception as e:
            logger.warning("Error in /add: {}", e)
            await message.answer("Error occurred.")

    async def _fsm_add_name(self, message: Message, state: FSMContext) -> None:
        try:
            if message.from_user is None:
                return
            name = message.text.strip().lower()
            if not name:
                await message.answer("Name cannot be empty. Enter the metric name:")
                return
            user_id = message.from_user.id
            existing = await self.db.get_metric_by_name(user_id, name)
            if existing is not None:
                await message.answer(f'Metric "{name}" already exists. Enter a different name:')
                return
            await state.update_data(metric_name=name)
            await state.set_state(AddStates.waiting_for_time)
            await message.answer(
                f"Metric: {name}\nEnter daily reminder time in HH:MM format, or send — to skip:"
            )
        except Exception as e:
            logger.warning("Error in FSM add name: {}", e)
            await message.answer("Error occurred.")

    async def _fsm_add_time(self, message: Message, state: FSMContext) -> None:
        try:
            if message.from_user is None:
                return
            text = message.text.strip()
            data = await state.get_data()
            name = data["metric_name"]
            user_id = message.from_user.id

            remind_time = None
            if text.strip() not in ("—", "-", "skip", "none", ""):
                if not re.match(r'^\d{2}:\d{2}$', text):
                    await message.answer("Invalid format. Enter HH:MM or — to skip:")
                    return
                try:
                    datetime.strptime(text, "%H:%M")
                    remind_time = text
                except ValueError:
                    await message.answer("Invalid time. Enter HH:MM or — to skip:")
                    return

            result = await self.db.add_metric(user_id, name, remind_time)
            await state.clear()
            if result is None:
                await message.answer(f'Metric "{name}" already exists')
            elif remind_time:
                await message.answer(f"Added: {name}, reminder at {remind_time}")
            else:
                await message.answer(f"Added: {name} (no reminder)")
        except Exception as e:
            logger.warning("Error in FSM add time: {}", e)
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
            metrics = await self.db.get_metrics(user_id)
            if not metrics:
                await message.answer("No metrics yet. Use /add")
                return
            await state.set_state(TrackStates.waiting_for_metric)
            keyboard = _build_metrics_keyboard(metrics)
            await message.answer("Choose a metric:", reply_markup=keyboard)
        except Exception as e:
            logger.warning("Error in /track: {}", e)
            await message.answer("Error occurred.")

    async def _fsm_track_metric(self, message: Message, state: FSMContext) -> None:
        try:
            if message.from_user is None:
                return
            user_id = message.from_user.id
            name = message.text.strip().lower()
            metric = await self.db.get_metric_by_name(user_id, name)
            if metric is None:
                await message.answer(f'Metric "{name}" not found. Choose from the list or send the name:')
                return
            await state.update_data(metric_id=metric["id"], metric_name=metric["name"])
            await state.set_state(TrackStates.waiting_for_value)
            keyboard = _build_value_keyboard(metric["id"])
            history = await self.db.get_last_records(metric["id"])
            history_str = _format_history(history)
            await message.answer(
                f"How is your {metric['name']}? (-5 — very bad, +5 — great)\nLast 10: {history_str}",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning("Error in FSM track metric: {}", e)
            await message.answer("Error occurred.")

    async def _cb_pick_metric(self, callback: CallbackQuery, state: FSMContext) -> None:
        try:
            if callback.from_user is None:
                await callback.answer()
                return
            parts = callback.data.split(":", 2)
            metric_id = int(parts[1])
            metric_name = parts[2]
            user_id = callback.from_user.id
            metric = await self.db.get_metric_by_id(metric_id)
            if metric is None or metric["user_id"] != user_id:
                await callback.answer("Not your metric")
                return
            await state.update_data(metric_id=metric_id, metric_name=metric_name)
            await state.set_state(TrackStates.waiting_for_value)
            keyboard = _build_value_keyboard(metric_id)
            history = await self.db.get_last_records(metric_id)
            history_str = _format_history(history)
            await callback.message.edit_text(
                f"How is your {metric_name}? (-5 — very bad, +5 — great)\nLast 10: {history_str}",
                reply_markup=keyboard,
            )
            await callback.answer()
        except Exception as e:
            logger.warning("Error in callback pick_metric: {}", e)
            await callback.answer("Error")

    async def _fsm_value_input(self, message: Message, state: FSMContext) -> None:
        try:
            if message.from_user is None:
                return
            text = message.text.strip()
            try:
                value = int(text)
            except ValueError:
                await message.answer("Please send a number from -5 to 5")
                return
            if value < -5 or value > 5:
                await message.answer("Please send a number from -5 to 5")
                return
            data = await state.get_data()
            metric_id = data["metric_id"]
            metric_name = data["metric_name"]
            user_id = message.from_user.id
            await self.db.add_record(user_id, metric_id, value)
            await state.clear()
            await message.answer(f"✅ {metric_name}: {value:+d}")
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
            await self.db.add_record(user_id, metric_id, value)
            metric_name = metric["name"]
            await callback.message.edit_text(f"✅ {metric_name}: {value:+d}", reply_markup=None)
            await callback.answer()
            current_state = await state.get_state()
            if current_state == TrackStates.waiting_for_value:
                await state.clear()
        except Exception as e:
            logger.warning("Error in callback record: {}", e)
            await callback.answer("Error")
