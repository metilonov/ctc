import asyncio
import io
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

from scheduler.planner import batches, reservation
from torrent.engine import metadata_from_magnet
from torrent.inputs import MAX_METADATA, fetch_torrent


def human(n):
    return f'{n / 1024**3:.2f} ГиБ'


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner):
        self.owner = owner

    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        message = getattr(event, 'message', None)
        chat = getattr(event, 'chat', None) or getattr(message, 'chat', None)
        if user and user.id == self.owner and chat and chat.type == 'private':
            return await handler(event, data)


def make_dispatcher(service):
    db, ws, s = service.db, service.ws, service.s
    router = Router()
    router.message.outer_middleware(OwnerOnly(s.owner))
    router.callback_query.outer_middleware(OwnerOnly(s.owner))
    cooldown = {}
    ingestion = asyncio.Lock()
    menu = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text='Добавить торрент'), KeyboardButton(text='Мои загрузки')],
        [KeyboardButton(text='Подключить Google Drive')],
        [KeyboardButton(text='Статус сервера'), KeyboardButton(text='Настройки')]], resize_keyboard=True)

    def job_buttons(job):
        return keyboard([
            [('Пауза', f'pause:{job}'), ('Продолжить', f'run:{job}')],
            [('Отменить', f'cancel:{job}'), ('Список файлов', f'files:{job}:0')],
            [('Обновить статус', f'status:{job}')]])

    def status(job):
        j = db.one('SELECT * FROM jobs WHERE id=?', (job,))
        fs = db.all('SELECT * FROM files WHERE job=?', (job,))
        selected = [f for f in fs if f['selected']]
        size = sum(f['size'] for f in selected)
        sent = sum(f['size'] for f in selected if f['state'] in ('done', 'confirmed'))
        local = sum(f['progress'] for f in selected if f['state'] not in ('done', 'confirmed'))
        down = sum(v[0] for k, v in service.rates.items() if k[0] == job)
        up = sum(v[1] for k, v in service.rates.items() if k[0] == job)
        text = (f"Название: {j['name'][:250]}\nID: {job}\n"
                f"Общий размер: {human(sum(f['size'] for f in fs))}\n"
                f"Рабочее пространство: {human(s.limit)}\nВыбрано файлов: {len(selected)}\n"
                f"Статус: {j['state']}\nСкачано/передано: {(sent + local) / max(1, size):.1%}\n"
                f"Подтверждено Drive: {sent / max(1, size):.1%}\n"
                f"Успешных файлов: {sum(f['state'] == 'done' for f in selected)}\n"
                f"В очереди: {sum(f['state'] == 'queued' for f in selected)}\n"
                f"Оценка партий: {batches(selected, s.limit - s.reserve)}\n"
                f"Загрузка: {down / 1024**2:.2f} МиБ/с; Drive: {up / 1024**2:.2f} МиБ/с\n"
                f"На диске (все задачи): {human(ws.used())}\nETA: недостаточно данных")
        for f in selected:
            if f['state'] in ('downloading', 'uploading', 'error') or f['error']:
                text += (f"\n{f['idx']}: {f['state']} ↓{f['progress']/max(1,f['size']):.0%} "
                         f"↑{f['uploaded']/max(1,f['size']):.0%} {f['error'] or ''}")
        return text[:3900]

    async def show_files(message, job, page):
        j = db.one('SELECT state FROM jobs WHERE id=?', (job,))
        fs = db.all('SELECT * FROM files WHERE job=? ORDER BY idx', (job,))
        page = max(0, min(page, max(0, (len(fs) - 1) // 8)))
        rows = []
        editable = j['state'] == 'selecting'
        lines = [f'Файлы {job}; страница {page + 1}/{max(1,(len(fs)+7)//8)}']
        for f in fs[page * 8:page * 8 + 8]:
            fits = reservation(f) <= s.limit - s.reserve
            lines.append(f"{f['idx']}: {f['path'][:250]} ({human(f['size'])})"
                         + ('' if fits else ' — не помещается с резервом'))
            if editable and fits:
                rows.append([(f"{'✓' if f['selected'] else '○'} {f['idx']}",
                              f"toggle:{job}:{f['idx']}:{page}")])
        rows.append([('←', f'files:{job}:{page-1}'), ('→', f'files:{job}:{page+1}')])
        if editable:
            rows.append([('Все допустимые', f'all:{job}'), ('Начать', f'run:{job}')])
        rows.append([('Статус', f'status:{job}')])
        await message.answer('\n'.join(lines), reply_markup=keyboard(rows))

    @router.message(CommandStart())
    async def start(message):
        await message.answer('Cloud Torrent Bot. Отправьте magnet, .torrent или HTTPS-ссылку.', reply_markup=menu)

    @router.message(F.text == 'Добавить торрент')
    async def add_hint(message):
        await message.answer('Отправьте .torrent до 8 МиБ, magnet или прямую HTTPS-ссылку без перенаправлений.')

    @router.message(F.text == 'Мои загрузки')
    async def jobs(message):
        js = db.all('SELECT id,name,state FROM jobs ORDER BY created DESC LIMIT 20')
        await message.answer('Последние задачи' if js else 'Очередь пуста', reply_markup=keyboard([
            [(f"{j['name'][:40]} ({j['state']})", f"status:{j['id']}")] for j in js]) if js else None)

    @router.message(F.text == 'Подключить Google Drive')
    async def drive(message):
        await message.answer('OAuth настраивается на вашем компьютере: python -m drive.auth '
            '--client-secret client_secret.json. Перенесите google-token.enc в DATA_DIR по защищённому каналу '
            'и установите тот же TOKEN_ENCRYPTION_KEY. После переподключения нажмите Продолжить. '
            'Инструкция: docs/OAUTH.md. Не отправляйте секреты в Telegram.')

    @router.message(F.text == 'Статус сервера')
    async def server(message):
        reserved = db.one('SELECT COALESCE(SUM(reservation),0) n FROM files')['n']
        await message.answer(f'Занято: {human(ws.used())}; резерв файлов: {human(reserved)}\n'
                             f'Свободно на разделе: {human(ws.free())}\n'
                             f'Защита диска: {service.disk_blocked}; активных: {len(service.workers)}')

    @router.message(F.text == 'Настройки')
    async def settings(message):
        await message.answer(f'Лимит: {human(s.limit)}; технический резерв: {human(s.reserve)}; '
            f'параллельных файлов: {s.slots}; попыток: {s.attempts}. Настройки меняются через .env и перезапуск.')

    @router.message(Command('tree'))
    async def tree(message):
        parts = message.text.split()
        if len(parts) != 2 or not db.one('SELECT id FROM jobs WHERE id=?', (parts[-1],)):
            await message.answer('Использование: /tree ID')
            return
        fs = db.all('SELECT path,size FROM files WHERE job=? ORDER BY path', (parts[-1],))
        data = '\n'.join(f"{f['path']}\t{f['size']} bytes" for f in fs).encode()
        await message.answer_document(BufferedInputFile(data, filename='files.txt'))

    @router.callback_query()
    async def callback(query):
        await query.answer()
        now = time.monotonic()
        if now - cooldown.get(query.from_user.id, 0) < 2:
            return
        cooldown[query.from_user.id] = now
        parts = (query.data or '').split(':')
        if len(parts) < 2:
            return
        action, job = parts[:2]
        j = db.one('SELECT * FROM jobs WHERE id=?', (job,))
        if not j:
            return
        try:
            if action == 'files':
                await show_files(query.message, job, int(parts[2]))
                return
            if action in ('toggle', 'all') and j['state'] == 'selecting':
                for f in db.all('SELECT * FROM files WHERE job=?', (job,)):
                    if reservation(f) <= s.limit - s.reserve and (action == 'all' or f['idx'] == int(parts[2])):
                        db.update_file(job, f['idx'], selected=1 if action == 'all' else 1-f['selected'])
                await show_files(query.message, job, int(parts[3]) if action == 'toggle' else 0)
                return
            if action == 'run':
                service.start(job)
            if action == 'pause':
                await service.control(job, 'paused')
            if action == 'cancel':
                await service.control(job, 'cancelled')
            await query.message.answer(status(job), reply_markup=job_buttons(job))
        except (ValueError, OSError):
            await query.message.answer('Операция не выполнена. Проверьте выбор файлов, состояние диска и задачи.')

    @router.message()
    async def receive(message, bot: Bot):
        if ingestion.locked():
            await message.answer('Дождитесь обработки предыдущих метаданных.')
            return
        async with ingestion:
            try:
                if message.document:
                    doc = message.document
                    if not (doc.file_name or '').lower().endswith('.torrent') or not doc.file_size or doc.file_size > MAX_METADATA:
                        raise ValueError('Expected .torrent up to 8 MiB')
                    target = io.BytesIO()
                    await bot.download(doc, destination=target)
                    data = target.getvalue()
                elif message.text and message.text.startswith('magnet:?'):
                    await message.answer('Получаю метаданные; ожидание до 120 секунд.')
                    data = await metadata_from_magnet(message.text, ws.root / 'metadata')
                elif message.text and message.text.startswith('https://'):
                    data = await fetch_torrent(message.text)
                else:
                    await message.answer('Используйте меню или отправьте торрент.')
                    return
                job = service.add(data)
                await message.answer(status(job), reply_markup=job_buttons(job))
                await show_files(message, job, 0)
            except Exception as exc:
                # Avoid leaking URLs, tokens or raw libtorrent metadata in errors.
                await message.answer(f'Не удалось добавить торрент ({type(exc).__name__}). '
                    'Проверьте формат, URL, размер метаданных и безопасные имена файлов.')

    dp = Dispatcher()
    dp.include_router(router)
    return dp
