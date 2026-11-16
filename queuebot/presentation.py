"""One editable personal dashboard per queue and one editable group list."""
import html
from datetime import datetime
from queuebot.config import show_time, utc_iso, utc_now
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def esc(value):
    return html.escape(str(value), quote=False)


def ik(rows):
    """Inline buttons with native Telegram primary/success/danger colors.

    Cell forms: (label, callback); (label, callback, style);
    (label, URL, 'url'); (label, URL, 'url', style).
    """
    if not rows:
        return None
    markup = []
    for row in rows:
        buttons = []
        for cell in row:
            label, target = cell[:2]
            is_url = len(cell) >= 3 and cell[2] == 'url'
            style = (cell[3] if len(cell) >= 4 else None) if is_url else (cell[2] if len(cell) >= 3 else None)
            kwargs = {'url': target} if is_url else {'callback_data': target}
            if style in ('primary', 'success', 'danger'):
                kwargs['style'] = style
            buttons.append(InlineKeyboardButton(text=label, **kwargs))
        markup.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=markup)


LESSON_TIMES = ('09:20', '11:10', '13:45', '15:35')
WEEKDAYS_RU = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')


def lesson_label(utc_time, cfg):
    """Display calendar day and lesson number; don't fabricate a pair for other times."""
    local = datetime.fromisoformat(utc_time).astimezone(cfg.tz)
    short_time = local.strftime('%H:%M')
    try:
        pair = f'{LESSON_TIMES.index(short_time) + 1} пара'
    except ValueError:
        pair = 'вне расписания'
    return f"{local.strftime('%d.%m.%Y %H:%M')} ({WEEKDAYS_RU[local.weekday()]}, {pair})"


def short_name(name):
    return name[:42] + ('…' if len(name) > 42 else '')


def group_message(db, qid, cfg, bot_username):
    q, members = db.group_snapshot(qid)
    return render_group_message(q, members, cfg, bot_username)


def render_group_message(q, members, cfg, bot_username):
    """One public queue message, updated in place at each stage.

    The numeric prefix alone uses Telegram's monospace <code> styling.
    """
    if not q:
        return None, None
    qid = q['id']
    text = [f"<code>№{qid}</code> <b>Очередь: {esc(q['title'])}</b>",
            f"🗓 Пара: {lesson_label(q['lesson_at'], cfg)}",
            f"👥 Участников: {len(members)}"]
    link = f'https://t.me/{bot_username}?start=q_{qid}'
    if q['status'] == 'registration':
        text.append(f"⏳ Запись открыта до: {show_time(q['closes_at'], cfg)}")
        if not q['use_preferences']:
            text.append('🎲 Случайная жеребьёвка')
        text.extend(['', '<b>Записались:</b>'])
        text.extend(f"• {esc(short_name(m['name']))}" for m in members)
        if not members:
            text.append('— пока никого —')
        return '\n'.join(text), ik([[('✍️ Записаться / открыть очередь', link, 'url', 'success')]])
    if q['status'] == 'closed':
        text.append('☑️ <u>Завершена</u>')
    elif q['status'] == 'cancelled':
        text.append('❌ <u>Отменена</u>')
    if q['status'] != 'cancelled':
        text.extend(('', '<b>Порядок сдачи:</b>'))
        text.extend(f"{m['current_position']}. {esc(short_name(m['name']))}" for m in members)
        if not members:
            text.append('— никто не записался —')
    keyboard = ik([[('📩 Открыть действия в личке', link, 'url', 'primary')]]) if q['status'] == 'active' else None
    return '\n'.join(text), keyboard


def preference_label(member):
    return ('№' + str(member['desired'])) if member['pref_mode'] == 'exact' else 'случайное место'


def personal_message(db, qid, uid, cfg, view='main', notice=None):
    q = db.queue(qid)
    if not q:
        return 'Очередь не найдена.', None
    member = db.one('SELECT * FROM participants WHERE queue_id=? AND user_id=?', (qid, uid))
    text = [f"📚 <b>{esc(q['title'])}</b>", f"🗓 {show_time(q['lesson_at'], cfg)} · очередь №{qid}"]
    if notice:
        text.extend(['', f'ℹ️ {esc(notice)}'])
    if not member or member['status'] != 'joined':
        text.extend(['', 'Ты сейчас не участвуешь в этой очереди.'])
        if q['status'] == 'registration' and utc_iso(utc_now()) < q['closes_at']:
            return '\n'.join(text), ik([[('✍️ Записаться снова', f'rejoin:{qid}', 'success')]])
        if (member and member['status'] == 'withdrawn' and q['status'] == 'active'
                and utc_iso(utc_now()) < q['lesson_at']):
            text.append('Можно вернуться до начала пары. Ты окажешься в конце текущей очереди.')
            return '\n'.join(text), ik([[('↩️ Вернуться в конец очереди', f'rejoin:{qid}', 'success')]])
        return '\n'.join(text), None

    status = q['status']
    if status == 'registration':
        text.extend(['', '✅ Ты в очереди.'])
        if q['use_preferences']:
            text.append(f"🎯 Выбор: <b>{esc(preference_label(member))}</b>")
        text.append(f"⏳ Запись до {show_time(q['closes_at'], cfg)}")
        if view == 'number' and q['use_preferences']:
            text.extend(['', f'🔢 <b>Напиши число от 1 до {cfg.max_participants} в этот чат.</b>',
                         'Например: 3. Введённое сообщение будет удалено после обработки.',
                         'Если участников будет меньше, выбранный номер станет последним.'])
            return '\n'.join(text), ik([[('↩️ Отмена', f'show:{qid}')]])
        if view == 'leave_confirm':
            text.extend(['', 'Точно выйти из очереди? Штрафа за выход нет.'])
            return '\n'.join(text), ik([[('🚪 Да, выйти', f'leaveyes:{qid}', 'danger')], [('↩️ Назад', f'show:{qid}')]])
        buttons = []
        if q['use_preferences']:
            text.append('\nДо жеребьёвки можно менять выбор и выходить из очереди.')
            buttons.append([('🔢 Конкретный номер', f'exact:{qid}', 'primary'),
                            ('🎲 Случайное место', f'pref:{qid}:any', 'success')])
        else:
            text.append('\n🎲 Место определит случайная жеребьёвка.')
        buttons.append([('🚪 Выйти из очереди', f'leave:{qid}', 'danger')])
        return '\n'.join(text), ik(buttons)

    if status == 'active':
        text.extend(['', f"🎫 <b>Твоё место: №{member['current_position']}</b>"])
        incoming = db.one("""SELECT sw.id, u.name FROM swaps sw JOIN users u ON u.id=sw.from_user
            WHERE sw.queue_id=? AND sw.to_user=? AND sw.status='pending' AND sw.expires_at>?
            ORDER BY sw.id DESC LIMIT 1""", (qid, uid, utc_iso(utc_now())))
        outgoing = db.one("""SELECT sw.id, u.name FROM swaps sw JOIN users u ON u.id=sw.to_user
            WHERE sw.queue_id=? AND sw.from_user=? AND sw.status='pending' AND sw.expires_at>?
            ORDER BY sw.id DESC LIMIT 1""", (qid, uid, utc_iso(utc_now())))
        if incoming:
            text.extend(['', f"🔁 <b>{esc(incoming['name'])}</b> предлагает обменяться местами.",
                         'Предложение действует 5 минут.'])
            return '\n'.join(text), ik([[('✅ Принять', f"swreply:{incoming['id']}:yes", 'success'),
                                         ('❌ Отклонить', f"swreply:{incoming['id']}:no", 'danger')]])
        if outgoing:
            text.extend(['', f"⏳ Ожидаем ответ от <b>{esc(outgoing['name'])}</b>."])
            return '\n'.join(text), ik([[('🔙 Назад', f'show:{qid}')]])
        if view == 'leave_confirm':
            text.extend(['', 'Точно выйти? Все следующие участники сместятся вверх на одну позицию.',
                         'Штрафа за выход нет.'])
            return '\n'.join(text), ik([[('🚪 Да, выйти', f'leaveyes:{qid}', 'danger')], [('↩️ Назад', f'show:{qid}')]])
        if view == 'swaps':
            others = [r for r in db.members(qid) if r['user_id'] != uid]
            text.extend(['', '🔁 Выбери, кому предложить обмен (5 минут на ответ):'])
            rows = [[(f"№{r['current_position']} {r['name'][:32]}", f"swap:{qid}:{r['user_id']}")]
                    for r in others]
            rows.append([('↩️ Назад', f'show:{qid}')])
            return '\n'.join(text), ik(rows)
        text.append('\nМожно обменяться местами или выйти из очереди.')
        return '\n'.join(text), ik([[('🔁 Предложить обмен', f'swaps:{qid}', 'primary')],
                                    [('🚪 Выйти из очереди', f'leave:{qid}', 'danger')]])

    text.append('\n✅ Очередь завершена.' if status == 'closed' else '\n❌ Очередь отменена.')
    return '\n'.join(text), None
