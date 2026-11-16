"""Telegram handlers. Each participant has one editable private dashboard per queue."""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from weakref import WeakValueDictionary
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    BotCommandScopeAllPrivateChats, BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats, BotCommandScopeDefault,
)
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from queuebot.config import utc_now, utc_iso
from queuebot.presentation import render_group_message, personal_message, ik
from queuebot.service import DomainError

logger = logging.getLogger(__name__)
router = Router()
DB = CFG = SERVICE = None
BOT_NAME = ''
GROUP_TYPES = {'group', 'supergroup'}
GROUP_LOCKS = WeakValueDictionary()
PANEL_LOCKS = WeakValueDictionary()
TERMINAL_CARD_TEXT = {
    'closed': '✅ Пара завершена. Итоговая очередь сохранена в группе.',
    'cancelled': '❌ Очередь отменена.',
}


def setup(db, cfg, service, bot_name):
    global DB, CFG, SERVICE, BOT_NAME
    DB, CFG, SERVICE, BOT_NAME = db, cfg, service, bot_name


def command_args(message):
    parts = (message.text or '').split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ''


async def delete_input(bot, message):
    """Remove handled commands from either a public or an ephemeral chat timeline.

    Ephemeral group commands are never shown to other users. We additionally
    try to remove them from the sender's own timeline after processing.
    Ordinary group commands are deleted as a backward-compatible fallback.
    """
    ephemeral_id = getattr(message, 'ephemeral_message_id', None)
    if ephemeral_id:
        receiver = getattr(message, 'receiver_user', None)
        uid = getattr(receiver, 'id', None) or getattr(getattr(message, 'from_user', None), 'id', None)
        if uid is not None:
            try:
                await bot.delete_ephemeral_message(
                    chat_id=message.chat.id,
                    receiver_user_id=uid,
                    ephemeral_message_id=ephemeral_id,
                )
            except (TelegramAPIError, OSError) as exc:
                logger.debug('Cannot remove ephemeral command: %s', exc)
        return
    if not getattr(message, 'message_id', None):
        return
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except (TelegramAPIError, OSError) as exc:
        logger.debug('Cannot delete user input (grant Delete Messages in group): %s', exc)


async def safe_send(bot, uid, text, markup=None):
    try:
        return await bot.send_message(uid, text, reply_markup=markup, parse_mode='HTML')
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning('Cannot send DM to %s: %s', uid, exc)
    return None


async def register_commands(bot):
    """Register private group slash commands (Bot API 10.3+).

    If a client sends a regular, public command anyway, handlers delete it as
    before. Real ephemeral user commands are never publicly visible.
    """
    private = [
        ('start', 'Открыть бота'),
        ('my', 'Мои активные очереди'),
        ('info', 'Описание работы бота'),
        ('help', 'Помощь'),
    ]
    group_admin = [
        ('queue', 'Создать очередь: /queue [предмет] дата время срок [0 или 1]'),
        ('close_reg', 'Закрыть запись: /close_reg ID'),
        ('finish_lesson', 'Завершить пару: /finish_lesson ID'),
        ('cancel', 'Отменить очередь: /cancel ID'),
    ]
    await bot.set_my_commands(
        [BotCommand(command=command, description=description) for command, description in private],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [BotCommand(command=command, description=description, is_ephemeral=True)
         for command, description in group_admin],
        scope=BotCommandScopeAllChatAdministrators(),
    )
    # Ordinary members see no bot commands in the group's slash menu.
    # Runtime permission checks below still apply to commands typed manually.
    await bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())
    # Clear old BotFather/default menus, which could otherwise leak into groups.
    await bot.set_my_commands([], scope=BotCommandScopeDefault())


async def update_group(bot, qid):
    lock = GROUP_LOCKS.setdefault(qid, asyncio.Lock())
    async with lock:
        q, members = DB.group_snapshot(qid)
        if not q or not q['group_message_id'] or q['group_synced_revision'] >= q['revision']:
            return
        if q['group_retry_at'] and q['group_retry_at'] > utc_iso(utc_now()):
            return
        text, keyboard = render_group_message(q, members, CFG, BOT_NAME)
        try:
            await bot.edit_message_text(text, chat_id=q['chat_id'], message_id=q['group_message_id'],
                                        reply_markup=keyboard, parse_mode='HTML')
        except (TelegramAPIError, OSError) as exc:
            if not isinstance(exc, TelegramBadRequest) or 'message is not modified' not in str(exc).lower():
                delay = max(30, exc.retry_after) if isinstance(exc, TelegramRetryAfter) else 30
                retry_at = utc_iso(utc_now() + timedelta(seconds=delay))
                DB.exec('UPDATE queues SET group_retry_at=? WHERE id=?', (retry_at, qid))
                logger.warning('Cannot update public queue %s; will retry: %s', qid, exc)
                return
        # A concurrent action may have bumped revision while Telegram was
        # processing the request. Acknowledge only the revision we rendered.
        DB.exec('UPDATE queues SET group_synced_revision=?,group_retry_at=NULL WHERE id=?',
                (q['revision'], qid))


async def publish_group(bot, qid):
    q, members = DB.group_snapshot(qid)
    try:
        text, markup = render_group_message(q, members, CFG, BOT_NAME)
        topic = {'message_thread_id': q['message_thread_id']} if q['message_thread_id'] is not None else {}
        posted = await bot.send_message(q['chat_id'], text, reply_markup=markup, parse_mode='HTML', **topic)
    except Exception:
        SERVICE.cancel(qid)
        raise
    DB.exec('UPDATE queues SET group_message_id=?,group_synced_revision=? WHERE id=?',
            (posted.message_id, q['revision'], qid))


def panel_row(qid, uid):
    return DB.one('SELECT * FROM personal_panels WHERE queue_id=? AND user_id=?', (qid, uid))


def save_view(qid, uid, view):
    DB.exec('UPDATE personal_panels SET view=? WHERE queue_id=? AND user_id=?', (view, qid, uid))


async def refresh_panel(bot, qid, uid, view=None, notice=None, create_if_missing=True):
    """Edit one saved personal card per queue; optionally forbid sending any new card.

    Background events such as a swap offer never send a separate private message.
    They only update the card already displayed in the recipient's chat.
    """
    lock = PANEL_LOCKS.setdefault((qid, uid), asyncio.Lock())
    async with lock:
        return await _refresh_panel(bot, qid, uid, view, notice, create_if_missing)


async def _refresh_panel(bot, qid, uid, view, notice, create_if_missing):
    q = DB.queue(qid)
    if not q or q['status'] in TERMINAL_CARD_TEXT:
        return False
    current = panel_row(qid, uid)
    selected = view if view is not None else (current['view'] if current else 'main')
    if (selected == 'swaps' and q['status'] != 'active') or (selected == 'leave_confirm' and q['status'] not in ('registration', 'active')):
        selected = 'main'
    if selected == 'number' and (q['status'] != 'registration' or not q['use_preferences']):
        selected = 'main'
    if selected == 'stats':  # Handle dashboards left in the SQLite DB by v2.5.
        selected = 'main'
    if current and selected != current['view']:
        save_view(qid, uid, selected)
    if selected in ('help', 'info'):
        text, markup = INFO, ik([[('↩️ К очереди', f'show:{qid}')]])
    else:
        text, markup = personal_message(DB, qid, uid, CFG, view=selected, notice=notice)
    if current:
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=current['message_id'],
                                        reply_markup=markup, parse_mode='HTML')
            return True
        except TelegramBadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return True
            error = str(exc).lower()
            if 'message to edit not found' not in error and 'message_id_invalid' not in error and "message can't be edited" not in error:
                logger.warning('Dashboard %s/%s edit failed: %s', qid, uid, exc)
                return False
            logger.info('Dashboard %s/%s was removed; creating a replacement', qid, uid)
        except TelegramForbiddenError:
            return False
    if not create_if_missing:
        logger.info('Dashboard %s/%s unavailable; no new message sent', qid, uid)
        return False
    sent = await safe_send(bot, uid, text, markup)
    if not sent:
        return False
    # Unique dashboard for each (queue,user); available after existing DB migration.
    DB.exec('''INSERT INTO personal_panels(queue_id,user_id,message_id,view) VALUES(?,?,?,?)
        ON CONFLICT(queue_id,user_id) DO UPDATE SET message_id=excluded.message_id,
        view=excluded.view''', (qid, uid, sent.message_id, selected))
    return True


async def refresh_members(bot, qid, create_if_missing=False):
    for member in DB.members(qid):
        await refresh_panel(bot, qid, member['user_id'], view='main',
                            create_if_missing=create_if_missing)


async def is_admin(bot, chat_id, uid):
    try:
        member = await bot.get_chat_member(chat_id, uid)
        return member.status in ('administrator','creator')
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def is_member(bot, chat_id, uid):
    try:
        member = await bot.get_chat_member(chat_id, uid)
        return member.status not in ('left','kicked')
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


INFO = (
    '📘 <b>Как работает бот очередей</b>\n\n'
    '1. Администратор создаёт очередь в нужной теме командой '
    '<code>/queue [предмет] дата время срок_записи [режим]</code>. '
    'Без предмета берётся известное название темы или название чата.\n'
    '2. Участник нажимает «Записаться» в группе. В режиме 1 (по умолчанию) '
    'в личке можно выбрать номер или случайное место и поменять выбор до закрытия записи. '
    'В режиме 0 достаточно записаться.\n'
    '3. После окончания записи бот проводит жеребьёвку. В режиме 1 он учитывает '
    'пожелания и историю жеребьёвок (не ниже 95% от оптимума). В режиме 0 '
    'порядок участников определяется случайно.\n'
    '4. После жеребьёвки можно предлагать обмены и выходить без штрафов. '
    'Если выйти до начала пары, можно вернуться в конец очереди.\n'
    '5. Ровно через 1 час 40 минут после начала пары очередь автоматически '
    'завершается. В группе итоговый список сохраняется, а личные карточки '
    'удаляются. Никаких опросов и переноса мест на следующую пару.\n'
    'Расписание пар: 1 — 09:20; 2 — 11:10; 3 — 13:45; 4 — 15:35.\n\n'
    '<b>В личке:</b> /start, /my, /info, /help.\n'
    '<b>Администраторы в группе:</b> /queue, /close_reg ID, /finish_lesson ID, /cancel ID.\n'
    'Групповые команды доступны только администраторам.'
)
HELP = ('📚 /info — подробное описание; /my — мои очереди. '
        'В группе только для администраторов: /queue, /close_reg, /finish_lesson, /cancel.')


@router.message(Command('help', 'info'), F.chat.type == 'private')
async def help_cmd(message: Message, bot: Bot):
    if message.from_user:
        uid = message.from_user.id
        content = INFO if (message.text or '').split('@')[0].startswith('/info') else HELP
        await safe_send(bot, uid, content,
                        ik([[('✅ Понятно', f'dismiss:{uid}', 'success')]]))
    await delete_input(bot, message)


@router.callback_query(F.data.startswith('dismiss:'))
async def dismiss_help(call: CallbackQuery, bot: Bot):
    # The button is only in the private chat of its intended recipient.
    owner = call.data.partition(':')[2]
    if not owner.isdigit() or int(owner) != call.from_user.id or (
        not call.message or call.message.chat.id != call.from_user.id
    ):
        await call.answer()
        return
    await call.answer()
    try:
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # Telegram cannot delete messages >48h. Make the card inert instead.
        logger.debug('Could not delete help card: %s', exc)
        try:
            await bot.edit_message_text('✅ Понятно', chat_id=call.message.chat.id,
                                        message_id=call.message.message_id, reply_markup=None)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


def parse_lesson_time(value):
    for fmt in ('%d.%m.%Y %H:%M', '%Y-%m-%d %H:%M'):
        try:
            local = datetime.strptime(value, fmt).replace(tzinfo=CFG.tz)
            return local.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            continue
    raise DomainError('Неверная дата или время начала пары.')


def parse_registration_hours(value):
    match = re.fullmatch(r'(\d+(?:[.,]\d+)?)(ч|h|m|м)?', value.lower())
    if not match:
        raise DomainError('Неверная длительность записи.')
    amount = float(match.group(1).replace(',', '.'))
    hours = amount / 60 if match.group(2) in ('m', 'м') else amount
    if not 0.05 <= hours <= 168:
        raise DomainError('Время записи должно быть от 3 минут до 7 дней.')
    return hours


def parse_queue_id(value):
    if re.fullmatch(r'[0-9]{1,19}', value):
        qid = int(value)
        if 0 < qid <= 2**63 - 1:
            return qid
    raise DomainError('Неверный ID очереди.')


def remember_topic_name(message):
    created = getattr(message, 'forum_topic_created', None)
    event = created or getattr(message, 'forum_topic_edited', None)
    title = getattr(event, 'name', None)
    if not title:
        return None
    thread_id = getattr(message, 'message_thread_id', None)
    if thread_id is None and created:
        thread_id = message.message_id
    if thread_id is None:
        return None
    updated_at = getattr(message, 'date', None) or utc_now()
    DB.remember_topic(message.chat.id, thread_id, title, utc_iso(updated_at))
    return title


@router.message(F.chat.type.in_(GROUP_TYPES), F.forum_topic_created)
@router.message(F.chat.type.in_(GROUP_TYPES), F.forum_topic_edited)
async def cache_topic_name(message: Message):
    remember_topic_name(message)


def default_queue_title(message, thread_id):
    if thread_id is not None:
        title = DB.topic_title(message.chat.id, thread_id)
        if title:
            return title
        reply = getattr(message, 'reply_to_message', None)
        if getattr(reply, 'message_id', None) == thread_id:
            title = remember_topic_name(reply)
            if title:
                return title
    return getattr(message.chat, 'title', None) or ''


@router.message(Command('queue'), F.chat.type.in_(GROUP_TYPES))
async def create_queue(message: Message, bot: Bot):
    try:
        parts = command_args(message).rsplit(maxsplit=4)
        use_preferences = True
        # A bare 1 following the lesson time is a duration in hours.
        if len(parts) >= 4 and parts[-1] in ('0', '1') and ':' in parts[-3]:
            use_preferences = parts.pop() == '1'
        if len(parts) < 3:
            return
        if not message.from_user or not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        date, time, duration = parts[-3:]
        lesson = parse_lesson_time(f'{date} {time}')
        hours = parse_registration_hours(duration)
        thread_id = getattr(message, 'message_thread_id', None)
        title = ' '.join(parts[:-3]) or default_queue_title(message, thread_id)
        qid = SERVICE.create(message.chat.id, message.from_user.id, title, lesson, hours,
                             message_thread_id=thread_id, use_preferences=use_preferences)
        await publish_group(bot, qid)
    except DomainError as exc:
        logger.debug('Queue command ignored in chat %s: %s', message.chat.id, exc)
    except (TelegramAPIError, OSError) as exc:
        logger.warning('Queue command failed in chat %s: %s', message.chat.id, exc)
    finally:
        await delete_input(bot, message)


async def admin_queue_action(message, bot, action):
    try:
        qid = parse_queue_id(command_args(message))
        if not message.from_user or not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        q = DB.queue(qid)
        if not q or q['chat_id'] != message.chat.id:
            return
        if action == 'close_reg':
            closed = DB.exec("""UPDATE queues SET closes_at=?,revision=revision+1
                WHERE id=? AND status='registration'""", (utc_iso(utc_now()), qid))
            if not closed:
                return
            await draw_and_notify(bot, qid)
        elif action == 'finish_lesson':
            if utc_iso(utc_now()) < q['lesson_at']:
                return
            await finish_and_notify(bot, qid)
        else:
            SERVICE.cancel(qid)
            await update_group(bot, qid)
            await cleanup_closed_panels(bot, qid)
        logger.info('Admin action %s for queue %s', action, qid)
    except DomainError as exc:
        logger.debug('Admin action %s ignored in chat %s: %s', action, message.chat.id, exc)
    except (TelegramAPIError, OSError, RuntimeError) as exc:
        logger.warning('Admin action %s failed in chat %s: %s', action, message.chat.id, exc)
    finally:
        await delete_input(bot, message)


@router.message(Command('close_reg'), F.chat.type.in_(GROUP_TYPES))
async def close_reg_cmd(message: Message, bot: Bot):
    await admin_queue_action(message, bot, 'close_reg')


@router.message(Command('finish_lesson'), F.chat.type.in_(GROUP_TYPES))
async def finish_lesson_cmd(message: Message, bot: Bot):
    await admin_queue_action(message, bot, 'finish_lesson')


@router.message(Command('cancel'), F.chat.type.in_(GROUP_TYPES))
async def cancel_cmd(message: Message, bot: Bot):
    await admin_queue_action(message, bot, 'cancel')


@router.message(Command('help', 'info', 'start', 'my', 'close', 'finish'), F.chat.type.in_(GROUP_TYPES))
async def discard_group_command(message: Message, bot: Bot):
    await delete_input(bot, message)


async def draw_and_notify(bot, qid):
    await asyncio.to_thread(SERVICE.draw, qid)
    await update_group(bot, qid)
    await refresh_members(bot, qid, create_if_missing=True)


async def finish_and_notify(bot, qid):
    SERVICE.finish(qid)
    # The group list is the archive: only its status is changed.
    await update_group(bot, qid)
    await cleanup_closed_panels(bot, qid)


@router.message(CommandStart(), F.chat.type=='private')
async def start_cmd(message: Message, bot: Bot):
    try:
        if not message.from_user:
            return
        args = command_args(message)
        if args and not args.startswith('q_'):
            return
        uid = message.from_user.id
        if not args:
            DB.user(message.from_user, utc_iso(utc_now()))
            latest = DB.one('SELECT queue_id FROM personal_panels WHERE user_id=? ORDER BY queue_id DESC LIMIT 1',
                            (uid,))
            if latest:
                await refresh_panel(bot, latest['queue_id'], uid, view='main')
            else:
                await message.answer('Привет! Чтобы записаться, нажми «Записаться» в сообщении группы. Команды: /my, /info.')
            return
        qid = parse_queue_id(args[2:])
        q = DB.queue(qid)
        if not q or not await is_member(bot, q['chat_id'], uid):
            return
        DB.user(message.from_user, utc_iso(utc_now()))
        if q['status'] == 'registration' and SERVICE.signup(qid, uid):
            await update_group(bot, qid)
        await refresh_panel(bot, qid, uid, view='main')
    except DomainError:
        pass
    finally:
        await delete_input(bot, message)


async def retire_personal_card(bot, qid, uid, message_id):
    """Delete a card or remove its buttons if Telegram forbids deletion by age."""
    lock = PANEL_LOCKS.setdefault((qid, uid), asyncio.Lock())
    async with lock:
        current = panel_row(qid, uid)
        if not current or current['message_id'] != message_id:
            return True
        terminal_text = TERMINAL_CARD_TEXT.get(DB.queue(qid)['status'])
        try:
            try:
                await bot.delete_message(chat_id=uid, message_id=message_id)
            except TelegramBadRequest as exc:
                error = str(exc).lower()
                if 'not found' not in error and 'message_id_invalid' not in error:
                    try:
                        await bot.edit_message_text(
                            terminal_text or 'ℹ️ Карточка обновлена командой /my.',
                            chat_id=uid, message_id=message_id, reply_markup=None,
                        )
                    except TelegramBadRequest as edit_exc:
                        if not any(reason in str(edit_exc).lower()
                                   for reason in ('not modified', 'not found', 'message_id_invalid')):
                            raise
        except TelegramForbiddenError:
            if not terminal_text:
                return False
        except (TelegramAPIError, OSError) as exc:
            logger.warning('Cannot retire card %s/%s; keeping its message ID: %s', qid, uid, exc)
            return False
        DB.exec('DELETE FROM personal_panels WHERE queue_id=? AND user_id=? AND message_id=?',
                (qid, uid, message_id))
        return True


@router.message(Command('my'), F.chat.type=='private')
async def my_cmd(message: Message, bot: Bot):
    if not message.from_user:
        return
    uid = message.from_user.id
    DB.user(message.from_user, utc_iso(utc_now()))
    # Delete previously sent queue cards (including withdrawn/completed ones),
    # then generate new cards only for queues in which the user is enrolled.
    old = DB.all('SELECT queue_id,message_id FROM personal_panels WHERE user_id=? ORDER BY queue_id', (uid,))
    for panel in old:
        await retire_personal_card(bot, panel['queue_id'], uid, panel['message_id'])
    rows = DB.all("""SELECT p.queue_id FROM participants p JOIN queues q ON q.id=p.queue_id
        WHERE p.user_id=? AND p.status='joined' AND q.status IN ('registration','active')
        ORDER BY q.lesson_at LIMIT 50""", (uid,))
    await delete_input(bot, message)
    if not rows:
        await safe_send(bot, uid, 'Сейчас ты не состоишь ни в одной активной очереди.',
                        ik([[('✅ Понятно', f'dismiss:{uid}', 'success')]]))
        return
    for row in rows:
        await refresh_panel(bot, row['queue_id'], uid, view='main')


@router.callback_query(F.data.startswith('show:'))
async def show_queue(call: CallbackQuery, bot: Bot):
    await call.answer()
    await refresh_panel(bot, int(call.data.split(':')[1]), call.from_user.id, view='main')


@router.callback_query(F.data.startswith('pref:'))
async def choose_pref(call: CallbackQuery, bot: Bot):
    _, raw, mode = call.data.split(':')
    if mode != 'any':
        await call.answer()
        return
    qid = int(raw)
    try:
        SERVICE.preference(qid, call.from_user.id, mode)
    except DomainError:
        await call.answer()
        return
    await call.answer('Выбрано случайное место')
    await refresh_panel(bot, qid, call.from_user.id, view='main')


@router.callback_query(F.data.startswith('exact:'))
async def ask_exact(call: CallbackQuery, bot: Bot):
    qid = int(call.data.split(':')[1])
    q = DB.queue(qid)
    user = DB.one("SELECT 1 FROM participants WHERE queue_id=? AND user_id=? AND status='joined'",
                  (qid, call.from_user.id))
    if (not q or not q['use_preferences'] or q['status'] != 'registration'
            or utc_iso(utc_now()) >= q['closes_at'] or not user):
        await call.answer()
        return
    # The DB-stored dashboard view survives bot restarts; no in-memory FSM needed.
    DB.exec("UPDATE personal_panels SET view='main' WHERE user_id=? AND view='number' AND queue_id!=?",
            (call.from_user.id, qid))
    await call.answer()
    await refresh_panel(bot, qid, call.from_user.id, view='number')


@router.callback_query(F.data.startswith('rejoin:'))
async def rejoin(call: CallbackQuery, bot: Bot):
    qid = int(call.data.split(':')[1])
    q = DB.queue(qid)
    if not q or not await is_member(bot, q['chat_id'], call.from_user.id):
        await call.answer()
        return
    try:
        if q['status'] == 'registration':
            SERVICE.signup(qid, call.from_user.id)
        else:
            SERVICE.rejoin(qid, call.from_user.id)
    except DomainError:
        await call.answer()
        return
    await call.answer('Ты снова в очереди')
    await refresh_panel(bot, qid, call.from_user.id, view='main')
    await update_group(bot, qid)
    await refresh_members(bot, qid)


@router.callback_query(F.data.startswith('leave:'))
async def ask_leave(call: CallbackQuery, bot: Bot):
    qid = int(call.data.split(':')[1])
    q = DB.queue(qid)
    if not q or q['status'] not in ('registration','active'):
        await call.answer()
        return
    await call.answer()
    await refresh_panel(bot, qid, call.from_user.id, view='leave_confirm')


@router.callback_query(F.data.startswith('leaveyes:'))
async def do_leave(call: CallbackQuery, bot: Bot):
    qid = int(call.data.split(':')[1])
    try:
        SERVICE.withdraw(qid, call.from_user.id)
    except DomainError:
        await call.answer()
        return
    await call.answer('Ты вышел из очереди')
    await update_group(bot, qid)
    await refresh_panel(bot, qid, call.from_user.id, view='main')
    await refresh_members(bot, qid)


@router.callback_query(F.data.startswith('swaps:'))
async def list_swaps(call: CallbackQuery, bot: Bot):
    qid = int(call.data.split(':')[1])
    q = DB.queue(qid)
    member = DB.one("SELECT 1 FROM participants WHERE queue_id=? AND user_id=? AND status='joined'",
                    (qid, call.from_user.id))
    if not q or q['status']!='active' or not member:
        await call.answer()
        return
    await call.answer()
    await refresh_panel(bot, qid, call.from_user.id, view='swaps')


@router.callback_query(F.data.startswith('swap:'))
async def offer_swap(call: CallbackQuery, bot: Bot):
    _, qid_s, target_s = call.data.split(':')
    qid, target = int(qid_s), int(target_s)
    # Swaps only from one's own private dashboard, and only with consent.
    try:
        sid = SERVICE.swap_request(qid, call.from_user.id, target)
    except DomainError:
        await call.answer()
        return
    # The receiver must have an existing personal dashboard. Never introduce
    # a second bot message when a swap is offered.
    sent = await refresh_panel(bot, qid, target, view='main', create_if_missing=False)
    if not sent:
        DB.exec("UPDATE swaps SET status='cancelled',handled_at=? WHERE id=? AND status='pending'",
                (utc_iso(utc_now()), sid))
        await call.answer()
        await refresh_panel(bot, qid, call.from_user.id, view='main', create_if_missing=False)
        return
    await call.answer('Предложение отправлено')
    await refresh_panel(bot, qid, call.from_user.id, view='main')


@router.callback_query(F.data.startswith('swreply:'))
async def respond_swap(call: CallbackQuery, bot: Bot):
    _, sid_s, decision = call.data.split(':')
    swap = DB.one('SELECT queue_id,from_user,to_user FROM swaps WHERE id=?',(int(sid_s),))
    try:
        qid, initiator, target = SERVICE.swap_decision(int(sid_s), call.from_user.id, decision=='yes')
    except DomainError:
        await call.answer()
        if swap and swap['to_user'] == call.from_user.id:
            await refresh_panel(bot, swap['queue_id'], swap['to_user'], view='main', create_if_missing=False)
            await refresh_panel(bot, swap['queue_id'], swap['from_user'], view='main', create_if_missing=False)
        return
    await call.answer('Обмен подтверждён' if decision == 'yes' else 'Предложение отклонено')
    if decision == 'yes':
        await update_group(bot, qid)
    for uid in (initiator, target):
        await refresh_panel(bot, qid, uid, view='main')


async def cleanup_closed_panels(bot, qid):
    """Retire cards of both completed and cancelled queues, including leavers."""
    q = DB.queue(qid)
    if not q or q['status'] not in TERMINAL_CARD_TEXT:
        return
    panels = DB.all('SELECT user_id,message_id FROM personal_panels WHERE queue_id=?', (qid,))
    for panel in panels:
        await retire_personal_card(bot, qid, panel['user_id'], panel['message_id'])


@router.message(F.chat.type=='private')
async def save_exact(message: Message, bot: Bot):
    """Capture the expected position from the user's personal queue card."""
    if not message.from_user:
        return
    pending = DB.one("SELECT queue_id FROM personal_panels WHERE user_id=? AND view='number' ORDER BY queue_id DESC LIMIT 1",
                     (message.from_user.id,))
    if not pending:
        return
    qid = pending['queue_id']
    try:
        number = int((message.text or '').strip())
        SERVICE.preference(qid, message.from_user.id, 'exact', number)
        await refresh_panel(bot, qid, message.from_user.id, view='main', notice='Номер сохранён.')
    except (ValueError, DomainError):
        pass
    finally:
        await delete_input(bot, message)
