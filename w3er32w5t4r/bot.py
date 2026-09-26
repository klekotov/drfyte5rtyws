from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
import asyncio
import logging
from typing import Dict, List, Optional

from config import TELEGRAM_TOKEN, SITES_CONFIG, CHECK_INTERVAL, INFERNO_EMAIL, INFERNO_PASSWORD
from database import Database
from parser import SiteParser
from payments import InfernoCookiesPayment, ALLOWED_PRODUCTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AddProduct(StatesGroup):
    selecting_site      = State()
    selecting_product   = State()
    custom_product_name = State()
    entering_qty        = State()
    confirm             = State()


class BuyNow(StatesGroup):
    selecting_item = State()
    entering_qty   = State()
    confirm        = State()


active_watchers: Dict[int, asyncio.Task] = {}


class AutoBuyBot:
    def __init__(self):
        self.bot     = Bot(token=TELEGRAM_TOKEN)
        self.storage = MemoryStorage()
        self.dp      = Dispatcher(storage=self.storage)
        self.db      = Database()
        self.parsers: Dict[str, SiteParser] = {}
        self.register_handlers()

    async def init(self):
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.db.init_db()
        for site_key, config in SITES_CONFIG.items():
            parser = SiteParser(config)
            await parser.init()
            self.parsers[site_key] = parser

    async def close(self):
        for parser in self.parsers.values():
            await parser.close()
        await self.bot.session.close()

    def register_handlers(self):
        self.dp.message.register(self.start_cmd,       Command("start"))
        self.dp.message.register(self.add_product_cmd, Command("add"))
        self.dp.message.register(self.my_products_cmd, Command("list"))
        self.dp.message.register(self.history_cmd,     Command("history"))
        self.dp.message.register(self.help_cmd,        Command("help"))
        self.dp.message.register(self.buy_now_cmd,     Command("buy"))

        self.dp.message.register(self.add_product_cmd, F.text == "➕ Добавить товар")
        self.dp.message.register(self.my_products_cmd, F.text == "📋 Мои товары")
        self.dp.message.register(self.history_cmd,     F.text == "📊 История")
        self.dp.message.register(self.help_cmd,        F.text == "❓ Справка")
        self.dp.message.register(self.buy_now_cmd,     F.text == "🛒 Купить")

        # AddProduct FSM
        self.dp.message.register(self.enter_custom_product, AddProduct.custom_product_name)
        self.dp.message.register(self.add_product_qty,      AddProduct.entering_qty)
        self.dp.callback_query.register(
            self.select_site, F.data.startswith("site_"),
            StateFilter(AddProduct.selecting_site)
        )
        self.dp.callback_query.register(
            self.select_product, F.data.startswith("prod_"),
            StateFilter(AddProduct.selecting_product)
        )
        self.dp.callback_query.register(
            self.confirm_product, F.data.startswith("confirm_"),
            StateFilter(AddProduct.confirm)
        )

        # BuyNow FSM
        self.dp.message.register(self.buynow_enter_qty,    BuyNow.entering_qty)
        self.dp.message.register(self.buynow_enter_custom, BuyNow.selecting_item,
                                  F.text & ~F.text.startswith("/"))
        self.dp.callback_query.register(
            self.buynow_select_item, F.data.startswith("bitem_"),
            StateFilter(BuyNow.selecting_item)
        )
        self.dp.callback_query.register(
            self.buynow_confirm, F.data.startswith("bconfirm_"),
            StateFilter(BuyNow.confirm)
        )

        # Stop / clear history
        self.dp.callback_query.register(
            self.stop_product, F.data.startswith("stop_")
        )
        self.dp.callback_query.register(
            self.clear_history_confirm, F.data.startswith("clrhist_")
        )

    # ------------------------------------------------------------------ #
    #  START                                                               #
    # ------------------------------------------------------------------ #
    async def start_cmd(self, message: types.Message):
        user_id  = message.from_user.id
        username = message.from_user.username or message.from_user.first_name
        await self.db.add_user(user_id, username)
        kb = ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="🛒 Купить")],
            [KeyboardButton(text="📋 Мои товары"),     KeyboardButton(text="📊 История")],
            [KeyboardButton(text="❓ Справка")]
        ], resize_keyboard=True)
        await message.answer(
            "🤖 <b>Auto Buy Bot</b>\n\nВыбери действие:",
            reply_markup=kb, parse_mode="HTML"
        )

    # ------------------------------------------------------------------ #
    #  ADD PRODUCT FSM                                                     #
    # ------------------------------------------------------------------ #
    async def add_product_cmd(self, message: types.Message, state: FSMContext):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=config['name'], callback_data=f"site_{key}")]
            for key, config in SITES_CONFIG.items()
        ])
        await message.answer("Выбери сайт для отслеживания:", reply_markup=kb)
        await state.set_state(AddProduct.selecting_site)

    async def select_site(self, callback: types.CallbackQuery, state: FSMContext):
        site_key = callback.data.replace("site_", "")
        if site_key not in SITES_CONFIG:
            await callback.answer("❌ Неизвестный сайт")
            return
        await state.update_data(site_key=site_key)

        buttons = [
            [InlineKeyboardButton(text=name, callback_data=f"prod_allowed_{name}")]
            for name in sorted(ALLOWED_PRODUCTS)
        ] + [[InlineKeyboardButton(text="✏️ Своё название", callback_data="prod_custom")]]

        await callback.message.edit_text(
            f"<b>{SITES_CONFIG[site_key]['name']}</b>\n\nВыбери товар или введи своё название:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.set_state(AddProduct.selecting_product)
        await callback.answer()

    async def select_product(self, callback: types.CallbackQuery, state: FSMContext):
        if callback.data == "prod_custom":
            await callback.message.edit_text(
                "✏️ Введи <b>точное название</b> товара с сайта:\n"
                "<i>Бот найдёт его в каталоге при покупке</i>",
                parse_mode="HTML"
            )
            await state.set_state(AddProduct.custom_product_name)
            await callback.answer()
            return

        if callback.data.startswith("prod_allowed_"):
            name = callback.data.replace("prod_allowed_", "")
            await state.update_data(selected_product={'name': name})
            await callback.message.edit_text(
                f"<b>{name}</b>\n\nСколько штук купить при появлении?",
                parse_mode="HTML"
            )
            await state.set_state(AddProduct.entering_qty)
            await callback.answer()
            return

        await callback.answer("❌ Неизвестный выбор")

    async def enter_custom_product(self, message: types.Message, state: FSMContext):
        name = message.text.strip()
        if len(name) < 2:
            await message.answer("❌ Слишком короткое название")
            return
        await state.update_data(selected_product={'name': name})
        await message.answer(
            f"Товар: <b>{name}</b>\n\nСколько штук купить при появлении?",
            parse_mode="HTML"
        )
        await state.set_state(AddProduct.entering_qty)

    async def add_product_qty(self, message: types.Message, state: FSMContext):
        try:
            qty = int(message.text.strip())
            if qty < 1:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введи целое число больше 0")
            return

        await state.update_data(buy_qty=qty)
        data         = await state.get_data()
        prod         = data.get('selected_product')
        product_name = prod['name'] if prod else data.get('custom_product_name', '')
        site_key     = data.get('site_key', '')
        site_name    = SITES_CONFIG.get(site_key, {}).get('name', site_key)

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_yes"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data="confirm_no")
        ]])
        await message.answer(
            f"<b>Подтверди добавление:</b>\n\n"
            f"Товар: {product_name}\n"
            f"Сайт: {site_name}\n"
            f"Куплю: <b>{qty} шт.</b> при появлении",
            reply_markup=kb, parse_mode="HTML"
        )
        await state.set_state(AddProduct.confirm)

    async def confirm_product(self, callback: types.CallbackQuery, state: FSMContext):
        if callback.data == "confirm_no":
            await callback.message.edit_text("❌ Отменено.")
            await state.clear()
            await callback.answer()
            return

        data         = await state.get_data()
        user_id      = callback.from_user.id
        site_key     = data['site_key']
        prod         = data.get('selected_product')
        product_name = prod['name'] if prod else data.get('custom_product_name')
        buy_qty      = data.get('buy_qty', 1)

        db_id = await self.db.add_watched_product(
            user_id, site_key, product_name,
            product_sku=None,
            target_price=None,
            buy_qty=buy_qty
        )
        await callback.message.edit_text(
            f"✅ <b>Товар добавлен!</b>\n\n"
            f"ID: {db_id}\n"
            f"Куплю {buy_qty} шт. при появлении\n"
            f"⚡ Проверяю каждые {CHECK_INTERVAL}с...",
            parse_mode="HTML"
        )
        await state.clear()
        await callback.answer()

        if user_id not in active_watchers:
            task = asyncio.create_task(self.watch_user_products(user_id))
            active_watchers[user_id] = task

    # ------------------------------------------------------------------ #
    #  MY PRODUCTS                                                         #
    # ------------------------------------------------------------------ #
    async def my_products_cmd(self, message: types.Message):
        user_id  = message.from_user.id
        products = await self.db.get_watched_products(user_id)
        if not products:
            await message.answer("📭 Ты пока ничего не отслеживаешь")
            return

        await message.answer("<b>📋 Мои товары:</b>", parse_mode="HTML")
        for p in products:
            site_name = SITES_CONFIG.get(p['site_key'], {}).get('name', 'Unknown')
            qty_line  = f" | Куплю: {p['buy_qty']} шт." if p.get('buy_qty') else ""
            text = (
                f"<b>#{p['id']}</b> — {p['product_name']}\n"
                f"Сайт: {site_name} | Статус: {p['status']}{qty_line}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⛔ Остановить", callback_data=f"stop_{p['id']}")
            ]])
            await message.answer(text, reply_markup=kb, parse_mode="HTML")

    async def stop_product(self, callback: types.CallbackQuery):
        product_id = int(callback.data.replace("stop_", ""))
        await self.db.remove_watched_product(product_id)
        await callback.message.edit_text(
            callback.message.text + "\n\n<i>⛔ Остановлено</i>",
            parse_mode="HTML",
            reply_markup=None
        )
        await callback.answer("⛔ Остановлено")

    # ------------------------------------------------------------------ #
    #  HISTORY                                                             #
    # ------------------------------------------------------------------ #
    async def history_cmd(self, message: types.Message):
        user_id   = message.from_user.id
        purchases = await self.db.get_purchase_history(user_id)
        if not purchases:
            await message.answer("📭 История покупок пуста")
            return

        text = "<b>📊 История покупок:</b>\n\n"
        for p in purchases:
            emoji = "✅" if p['status'] == 'success' else "❌"
            text += (
                f"{emoji} {p['product_name']}\n"
                f"  Заказ: {p['order_id']} | {p['purchased_at']}\n\n"
            )

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Очистить историю", callback_data="clrhist_confirm")
        ]])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    async def clear_history_confirm(self, callback: types.CallbackQuery):
        if callback.data == "clrhist_confirm":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да, очистить", callback_data="clrhist_yes"),
                InlineKeyboardButton(text="❌ Отмена",       callback_data="clrhist_no")
            ]])
            await callback.message.edit_text(
                "🗑 Очистить всю историю покупок?",
                reply_markup=kb
            )
            await callback.answer()
            return

        if callback.data == "clrhist_yes":
            user_id = callback.from_user.id
            await self.db.clear_purchase_history(user_id)
            await callback.message.edit_text("✅ История очищена.", reply_markup=None)
            await callback.answer("✅ Очищено")
            return

        if callback.data == "clrhist_no":
            await callback.message.edit_text("Отменено.", reply_markup=None)
            await callback.answer()

    # ------------------------------------------------------------------ #
    #  WATCHER                                                             #
    # ------------------------------------------------------------------ #
    async def _check_single_product(self, product: dict) -> Optional[tuple]:
        parser = self.parsers.get(product['site_key'])
        if not parser:
            return None
        try:
            check = await parser.check_stock(product['product_name'])
        except Exception as e:
            logger.error(f"Stock check [{product['product_name']}]: {e}")
            return None
        if not check.get('found') or not check['in_stock']:
            return None
        await self.db.update_product_cache(
            product['site_key'], product['product_name'], '',
            check['product'].get('price', 0),
            True,
            check.get('available_count', 0)
        )
        return (product, check)

    async def watch_user_products(self, user_id: int):
        logger.info(f"Watcher started: user {user_id}")
        try:
            while True:
                products = await self.db.get_watched_products(user_id, "active")
                if not products:
                    logger.info(f"No active products, watcher exits: user {user_id}")
                    break

                results      = await asyncio.gather(
                    *[self._check_single_product(p) for p in products],
                    return_exceptions=False
                )
                ready_to_buy = [r for r in results if r is not None]

                if ready_to_buy:
                    names_qty:   Dict[str, int]  = {}
                    product_map: Dict[str, dict] = {}
                    for product, _ in ready_to_buy:
                        name              = product['product_name']
                        names_qty[name]   = product.get('buy_qty') or 1
                        product_map[name] = product

                    await self.bot.send_message(
                        user_id,
                        "🔔 <b>ТОВАРЫ В НАЛИЧИИ!</b>\n\n"
                        + "\n".join(f"• {n} × {q} шт." for n, q in names_qty.items())
                        + "\n\n⚡ Получаю UUID и покупаю...",
                        parse_mode="HTML"
                    )

                    if not INFERNO_EMAIL or not INFERNO_PASSWORD:
                        await self.bot.send_message(
                            user_id,
                            "❌ INFERNO_EMAIL / INFERNO_PASSWORD не заданы в env."
                        )
                        await asyncio.sleep(CHECK_INTERVAL)
                        continue

                    payment         = InfernoCookiesPayment()
                    purchase_result = await payment.resolve_and_purchase(
                        names_qty, INFERNO_EMAIL, INFERNO_PASSWORD
                    )

                    resolved   = purchase_result.get('resolved', {})
                    unresolved = purchase_result.get('unresolved', [])

                    if unresolved:
                        await self.bot.send_message(
                            user_id,
                            f"⚠️ UUID не найден для: {', '.join(unresolved)}\n"
                            "Продолжаю мониторинг — повторю на следующей итерации."
                        )

                    for name, product in product_map.items():
                        uuid   = resolved.get(name)
                        status = 'success' if (purchase_result['success'] and uuid) else 'failed'
                        await self.db.add_purchase(
                            user_id, product['site_key'], name, 0,
                            status, purchase_result.get('order_id')
                        )
                        if purchase_result['success'] and uuid:
                            await self.db.remove_watched_product(product['id'])

                    if purchase_result['success']:
                        final_qty    = purchase_result.get('final_qty', sum(names_qty.values()))
                        wanted_qty   = sum(names_qty.values())
                        download_url = purchase_result.get('bundle_download_url', '')
                        msg = (
                            f"✅ <b>КУПЛЕНО!</b>\n\n"
                            f"Товаров: {final_qty}"
                        )
                        if final_qty < wanted_qty:
                            msg += f" <i>(хотели {wanted_qty} — не хватило баланса)</i>"
                        msg += f"\nЗаказ: {purchase_result['order_id']}\n\n"
                        if download_url:
                            msg += f"<a href='https://inferno-cookies.com{download_url}'>📥 Скачать</a>"
                        await self.bot.send_message(user_id, msg, parse_mode="HTML")
                    else:
                        await self.bot.send_message(
                            user_id,
                            f"❌ <b>Ошибка покупки:</b> {purchase_result.get('error', 'неизвестно')}\n"
                            "⚡ Продолжаю мониторинг...",
                            parse_mode="HTML"
                        )

                await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.CancelledError:
            logger.info(f"Watcher cancelled: user {user_id}")
        except Exception as e:
            logger.error(f"Watcher error user {user_id}: {e}", exc_info=True)
        finally:
            active_watchers.pop(user_id, None)

    # ------------------------------------------------------------------ #
    #  BUY NOW FSM                                                         #
    # ------------------------------------------------------------------ #
    async def buy_now_cmd(self, message: types.Message, state: FSMContext):
        if not INFERNO_EMAIL or not INFERNO_PASSWORD:
            await message.answer("❌ INFERNO_EMAIL / INFERNO_PASSWORD не заданы в env.")
            return

        buttons = [
            [InlineKeyboardButton(text=name, callback_data=f"bitem_{i}")]
            for i, name in enumerate(sorted(ALLOWED_PRODUCTS))
        ] + [[InlineKeyboardButton(text="✏️ Своё название", callback_data="bitem_custom")]]

        await message.answer(
            "📦 <b>Доступные товары</b>\nВыбери или введи своё название:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.update_data(
            catalog_items=[(name, name) for name in sorted(ALLOWED_PRODUCTS)],
            cart={},
            awaiting_custom=False
        )
        await state.set_state(BuyNow.selecting_item)

    async def buynow_select_item(self, callback: types.CallbackQuery, state: FSMContext):
        if callback.data == "bitem_custom":
            await callback.message.edit_text(
                "✏️ Введи <b>точное название</b> товара:",
                parse_mode="HTML"
            )
            await state.update_data(awaiting_custom=True)
            await callback.answer()
            return

        idx   = int(callback.data.replace("bitem_", ""))
        data  = await state.get_data()
        items: List[tuple] = data['catalog_items']
        if idx >= len(items):
            await callback.answer("❌ Ошибка")
            return
        name, uuid = items[idx]
        await state.update_data(selected_name=name, selected_uuid=uuid, awaiting_custom=False)
        await callback.message.edit_text(
            f"<b>{name}</b>\n\nСколько штук купить?",
            parse_mode="HTML"
        )
        await state.set_state(BuyNow.entering_qty)
        await callback.answer()

    async def buynow_enter_custom(self, message: types.Message, state: FSMContext):
        data = await state.get_data()
        if not data.get('awaiting_custom'):
            return
        name = message.text.strip()
        if len(name) < 2:
            await message.answer("❌ Слишком короткое название")
            return
        await state.update_data(selected_name=name, selected_uuid=name, awaiting_custom=False)
        await message.answer(
            f"Товар: <b>{name}</b>\n\nСколько штук купить?",
            parse_mode="HTML"
        )
        await state.set_state(BuyNow.entering_qty)

    async def buynow_enter_qty(self, message: types.Message, state: FSMContext):
        try:
            qty = int(message.text.strip())
            if qty < 1:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введи целое число больше 0")
            return

        data = await state.get_data()
        name = data['selected_name']
        uuid = data['selected_uuid']
        cart: dict = data.get('cart', {})
        if uuid in cart:
            cart[uuid]['qty'] += qty
        else:
            cart[uuid] = {'name': name, 'qty': qty}
        await state.update_data(cart=cart)

        cart_text = "\n".join(f"• {v['name']}: {v['qty']} шт." for v in cart.values())
        total     = sum(v['qty'] for v in cart.values())
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить ещё",          callback_data="bconfirm_more")],
            [InlineKeyboardButton(text=f"✅ Купить ({total} шт.)", callback_data="bconfirm_buy")],
            [InlineKeyboardButton(text="❌ Отмена",                callback_data="bconfirm_cancel")],
        ])
        await message.answer(
            f"🛒 <b>Корзина:</b>\n{cart_text}",
            reply_markup=kb, parse_mode="HTML"
        )
        await state.set_state(BuyNow.confirm)

    async def buynow_confirm(self, callback: types.CallbackQuery, state: FSMContext):
        action = callback.data.replace("bconfirm_", "")

        if action == "cancel":
            await callback.message.edit_text("❌ Отменено.")
            await state.clear()
            await callback.answer()
            return

        if action == "more":
            data  = await state.get_data()
            items = data['catalog_items']
            buttons = [
                [InlineKeyboardButton(text=name, callback_data=f"bitem_{i}")]
                for i, (name, _) in enumerate(items)
            ] + [[InlineKeyboardButton(text="✏️ Своё название", callback_data="bitem_custom")]]
            await callback.message.edit_text(
                "📦 Выбери ещё товар:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
            await state.update_data(awaiting_custom=False)
            await state.set_state(BuyNow.selecting_item)
            await callback.answer()
            return

        data    = await state.get_data()
        cart    = data.get('cart', {})
        user_id = callback.from_user.id
        if not cart:
            await callback.message.edit_text("❌ Корзина пустая.")
            await state.clear()
            await callback.answer()
            return

        names_qty: Dict[str, int] = {info['name']: info['qty'] for info in cart.values()}
        total = sum(names_qty.values())

        await callback.message.edit_text(f"⏳ Покупаю {total} шт....")
        await callback.answer()

        payment         = InfernoCookiesPayment()
        purchase_result = await payment.resolve_and_purchase(
            names_qty, INFERNO_EMAIL, INFERNO_PASSWORD
        )

        for info in cart.values():
            await self.db.add_purchase(
                user_id, "inferno", info['name'], 0,
                'success' if purchase_result['success'] else 'failed',
                purchase_result.get('order_id')
            )

        if purchase_result['success']:
            final_qty    = purchase_result.get('final_qty', total)
            download_url = purchase_result.get('bundle_download_url', '')
            msg = f"✅ <b>ПОКУПКА УСПЕШНА!</b>\n\nТоваров: {final_qty}"
            if final_qty < total:
                msg += f" <i>(хотели {total} — не хватило баланса)</i>"
            msg += f"\nЗаказ: {purchase_result['order_id']}\n\n"
            if download_url:
                msg += f"<a href='https://inferno-cookies.com{download_url}'>📥 Скачать</a>"
            await callback.message.edit_text(msg, parse_mode="HTML")
        else:
            await callback.message.edit_text(
                f"❌ <b>Ошибка:</b> {purchase_result.get('error', 'Неизвестная ошибка')}",
                parse_mode="HTML"
            )
        await state.clear()

    # ------------------------------------------------------------------ #
    #  HELP                                                                #
    # ------------------------------------------------------------------ #
    async def help_cmd(self, message: types.Message):
        await message.answer(
            "<b>❓ Справка</b>\n\n"
            "/add — Добавить товар для отслеживания\n"
            "/buy — Купить прямо сейчас\n"
            "/list — Мои товары\n"
            "/history — История покупок\n\n"
            "<b>Как работает автопокупка:</b>\n"
            "1️⃣ Добавь товар через ➕ (из списка или своё название)\n"
            f"2️⃣ Бот проверяет наличие каждые {CHECK_INTERVAL}с параллельно\n"
            "3️⃣ При появлении — покупает мгновенно\n"
            "4️⃣ Если не хватает баланса — уменьшает кол-во и повторяет до 5 раз\n"
            "5️⃣ Присылает ссылку на скачивание",
            parse_mode="HTML"
        )

    # ------------------------------------------------------------------ #
    #  RUN                                                                 #
    # ------------------------------------------------------------------ #
    async def run(self):
        await self.init()
        logger.info("Bot started (polling mode)")
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.close()


async def main():
    bot = AutoBuyBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())