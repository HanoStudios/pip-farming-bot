#!/usr/bin/env python3
"""PIP Farming control bot: private chat with @PipFarmingResultsBot (long polling; launchd com.pipfarming.bot keeps it alive).
Only the channel's and the group's human admins (+ PIP_ALLOWED_IDS in .env) can use it.
Menu: New post · Today's plan · Teach · Pause/Resume autopost."""
import os, sys, json, time, threading, datetime, traceback
import pipcore as P
import autopost as A

LOG = 'bot.log'
pending = {}            # user id -> what their next message means
plock = threading.Lock()
TYPE_ROWS = [[('Results', 'n:results'), ('Weekly', 'n:weekly'), ('Highlight', 'n:highlight')],
             [('Hype', 'n:hype'), ('Profits', 'n:profits'), ('Custom', 'n:custom')], [('Back', 'm:menu')]]

def log(*a): P.log(*a, f=LOG)
def send(chat, text, rows=None, **kw):
    return P.api('sendMessage', chat_id=chat, text=text, reply_markup=P.kb(rows) if rows else None, **kw)['message_id']
def edit(chat, mid, text, rows=None):
    try: P.api('editMessageText', chat_id=chat, message_id=mid, text=text, reply_markup=P.kb(rows) if rows else {'inline_keyboard': []})
    except P.TGError as e:
        if 'not modified' not in str(e): send(chat, text, rows)
def delete(chat, mid):
    try: P.api('deleteMessage', chat_id=chat, message_id=mid)
    except Exception: pass
def unbutton(chat, mid):
    try: P.api('editMessageReplyMarkup', chat_id=chat, message_id=mid, reply_markup={'inline_keyboard': []})
    except Exception: pass

def is_paused(): return os.path.exists(A.PAUSED)
def set_paused(on):
    if on: open(A.PAUSED, 'w').write(P.now().isoformat() + '\n')
    elif os.path.exists(A.PAUSED): os.remove(A.PAUSED)
    P.set_config(paused=on)

def pending_edit(date, t, **kw):
    """Skip / Change from the bot go into the schedule's hook file state/pending/HHMM.json (if it exists for that day)."""
    j = A.read_pending(date, t)
    if j:
        j.update(kw)
        with P.locked(): P.jwrite(A.pending_path(t), j)

def menu_rows():
    paused = is_paused()
    return [[('New post', 'm:new'), ("Today's plan", 'm:plan')], [('Teach', 'm:teach'), ('Resume autopost' if paused else 'Pause autopost', 'm:pause')]]
def menu(chat, mid=None):
    text = 'PIP Farming. Autopost is paused.' if is_paused() else 'PIP Farming. What do you need?'
    edit(chat, mid, text, menu_rows()) if mid else send(chat, text, menu_rows())

def busy(chat, fn, *a):
    """Runs a slow job (Claude + render) off the polling loop, with a short 'working' line that disappears after."""
    def run():
        wid = send(chat, 'Making it…'); t0 = time.time()
        try: fn(*a); log(f'job {getattr(fn, "__name__", "?")} took {time.time() - t0:.0f}s')
        except P.Stale as e: send(chat, f'Not made: {e}. Nothing was posted.')
        except Exception as e:
            log('job failed', repr(e), traceback.format_exc()[-600:]); send(chat, f'That failed: {str(e)[:150]}')
        finally: delete(chat, wid)
    threading.Thread(target=run, daemon=True).start()

# ---------- drafts from "New post" ----------
def draft_rows(d):
    did = d['id']
    if d.get('status') == 'scheduled': return [[('Post now', f'd:post:{did}'), ('Change', f'd:chg:{did}')], [('Unschedule', f'd:uns:{did}'), ('Drop', f'd:drop:{did}')]]
    return [[('Post now', f'd:post:{did}'), ('Change', f'd:chg:{did}')], [('Schedule', f'd:sch:{did}'), ('Drop', f'd:drop:{did}')]]

def show_draft(chat, d):
    for c, m in d.get('previews', []): delete(c, m)
    mid = P.api('sendDocument', files={'document': (f'pipfarming-{d["id"]}.png', d['png'])}, chat_id=chat, caption=d['caption'],
                reply_markup=P.kb(draft_rows(d)), http_timeout=120)['message_id']
    d = P.load_draft(d['id']); d['previews'] = [[chat, mid]]; P.save_draft(d)

def new_draft(chat, uid, kind, context, images):
    d = P.make_draft(kind, context=context, images=images, by=uid)
    show_draft(chat, d)

def change_draft(chat, did, note):
    d = P.load_draft(did)
    nd = P.make_draft(d['type'], context=d.get('context', ''), images=d.get('images', []), day=d.get('day'), by=d.get('by'), prev=d, note=note)
    show_draft(chat, nd)
    if nd.get('slot'): refresh_slot_previews(*slot_of(nd), nd)

# ---------- slots (the day plan) ----------
def slot_key(date, t): return f'{date}:{t.replace(":", "")}'
def parse_slot(k):
    date, hm = k.split(':'); return datetime.date.fromisoformat(date), f'{hm[:2]}:{hm[2:]}'
def slot_of(d): return parse_slot(d['slot'])

def refresh_slot_previews(date, t, d):
    """A slot's post changed: replace every admin's preview with the new version."""
    s = P.load_plan(date)[t]; new = []
    for chat, mid in s.get('previews', []):
        delete(chat, mid)
        try: new.append([chat, A.send_preview(chat, d, date, t, 'New version. ' + f'{t} · posts only after you approve.')])
        except Exception as e: log('preview resend failed', chat, e)
    P.update_slot(date, t, previews=new)

def change_slot(chat, date, t, note):
    s = P.load_plan(date)[t]
    if s['status'] not in ('planned', 'ready'): return send(chat, f'{t} is already {s["status"]}.')
    d = P.load_draft(s['draft']) if s.get('draft') else None
    if not d: s, d = A.build_for_slot(date, t, s)
    nd = P.make_draft(d['type'], context=d.get('context', ''), images=d.get('images', []), day=d.get('day'), by=d.get('by'), prev=d, note=note)
    pending_edit(date, t, caption=nd['caption'], png=nd['png']); P.update_slot(date, t, approved=False)   # a new version needs a new approval
    if not P.load_plan(date)[t].get('previews'):
        mid = A.send_preview(chat, nd, date, t); P.update_slot(date, t, previews=[[chat, mid]])
    else: refresh_slot_previews(date, t, nd)

def preview_slot(chat, date, t):
    s = P.load_plan(date)[t]
    d = P.load_draft(s['draft']) if s.get('draft') else None
    if not d: s, d = A.build_for_slot(date, t, s)
    mid = A.send_preview(chat, d, date, t, f'{t} · {P.slot_label(s)} · posts only after you approve.')
    P.update_slot(date, t, previews=P.load_plan(date)[t].get('previews', []) + [[chat, mid]])

STATUS = {'off': 'no post', 'planned': '', 'ready': 'preview sent', 'posted': 'posted', 'skipped': 'skipped', 'failed': 'failed', 'missed': 'missed', 'paused': 'paused', 'posting': 'posting'}
def plan_view(date=None):
    t0 = P.now(); date = date or t0.date(); plan = P.load_plan(date)
    if date == t0.date() and all(P.slot_dt(date, t) < t0 for t in P.SLOTS): date = date + datetime.timedelta(days=1); plan = P.load_plan(date)
    title = ('Today' if date == t0.date() else 'Tomorrow') + f', {date:%a %-d %b}' + (' · autopost paused' if is_paused() else '')
    lines, rows = [title], []
    for t in P.SLOTS:
        s = plan[t]; st = STATUS.get(s['status'], s['status'])
        if s.get('note') == 'before launch': st = 'before launch'
        elif s['status'] in ('planned', 'ready') and P.slot_dt(date, t) < t0: st = 'passed'
        lines.append(f"{t}  {P.slot_label(s)}{' (yours)' if s.get('manual') else ''}{'  · ' + st if st else ''}")
        if s['status'] in ('planned', 'ready') and P.slot_dt(date, t) > t0:
            k = slot_key(date, t); rows.append([(f'{t} Preview', f's:prev:{k}'), ('Skip', f's:skip:{k}'), ('Swap', f's:swap:{k}')])
        elif s['status'] == 'skipped' and P.slot_dt(date, t) > t0 and s.get('note') != 'before launch':
            rows.append([(f'{t} Restore', f's:unskip:{slot_key(date, t)}')])
    rows.append([('Menu', 'm:menu')])
    return '\n'.join(lines), rows

# ---------- teach ----------
def rules_text():
    r = P.guide_rules()
    return 'No rules yet.' if not r else '\n'.join(f'{i+1}. {x}' for i, x in enumerate(r))
TEACH_ROWS = [[('Show rules', 'g:show'), ('Clear a rule', 'g:clear')], [('Done', 'g:done')]]

# ---------- updates ----------
def on_message(m):
    chat = m['chat']['id']; uid = str(m['from']['id']); text = (m.get('text') or m.get('caption') or '').strip()
    with plock: st = pending.get(uid)
    if text in ('/start', '/menu', 'menu', 'Menu'):
        with plock: pending.pop(uid, None)
        return menu(chat)
    # a reply to a preview = a change note for it
    rt = (m.get('reply_to_message') or {}).get('message_id')
    if rt and text and not (st and st['mode'] == 'collect'):
        target = find_preview(chat, rt)
        if target: st = {'mode': 'change', **target}
    if st and st['mode'] == 'collect':
        if m.get('photo'): st['images'].append(P.download(m['photo'][-1]['file_id'], media_path(uid, 'jpg')))
        elif m.get('document') and str(m['document'].get('mime_type', '')).startswith('image/'):
            st['images'].append(P.download(m['document']['file_id'], media_path(uid, m['document'].get('file_name', 'img.png').rsplit('.', 1)[-1])))
        if text: st['notes'].append(text)
        n, k = len(st['notes']), len(st['images'])
        return edit(chat, st['msg'], f"{P.LABEL[st['type']]}: add context, images or links, then tap Done.\nGot {n} note{'s' * (n != 1)}, {k} image{'s' * (k != 1)}.", COLLECT_ROWS)
    if st and st['mode'] == 'plan_idea' and text:
        with plock: pending.pop(uid, None)
        with open(os.path.join(P.ST, 'plan-ideas.md'), 'a') as fh: fh.write(f"- {P.now():%Y-%m-%d %H:%M} {P.allowed().get(uid, uid)}: {text}\n")
        for other in P.allowed():
            if other != uid:
                try: send(int(other), f"{P.allowed().get(uid, 'Teammate')} suggested a different plan: {text}")
                except Exception: pass
        return send(chat, 'Got it. The plan will be updated. Nothing changes until then.')
    if st and st['mode'] == 'teach' and text:
        n = P.add_rule(text); return send(chat, f'Saved as rule {n}.', [[('Done', 'g:done')]])
    if st and st['mode'] == 'change' and text:
        with plock: pending.pop(uid, None)
        P.add_rule(f'Change note: {text}')
        if st.get('draft'): return busy(chat, change_draft, chat, st['draft'], text)
        date, t = parse_slot(st['slot']); return busy(chat, change_slot, chat, date, t, text)
    if text and not text.startswith('/') and text.lower() not in ('menu',):
        return threading.Thread(target=chat_reply, args=(chat, uid, text), daemon=True).start()
    menu(chat)

COLLECT_ROWS = [[('Done', 'c:done'), ('Cancel', 'c:cancel')]]
def media_path(uid, ext):
    d = os.path.join(P.ST, 'drafts', 'media'); os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'{uid}-{int(time.time() * 1000)}.{ext[:4]}')

def find_preview(chat, mid):
    for f in sorted(os.listdir(os.path.join(P.ST, 'drafts')))[-200:]:
        if f.endswith('.json'):
            d = P.jread(os.path.join(P.ST, 'drafts', f), {}) or {}
            if [chat, mid] in d.get('previews', []) and d.get('status') in ('draft', 'scheduled'): return {'draft': d['id']}
    t0 = P.now().date()
    for date in (t0, t0 + datetime.timedelta(days=1)):
        for t, s in P.load_plan(date).items():
            if [str(chat), mid] in [[str(a), b] for a, b in s.get('previews', [])]: return {'slot': slot_key(date, t)}

def on_callback(q):
    chat = q['message']['chat']['id']; mid = q['message']['message_id']; uid = str(q['from']['id']); data = q.get('data', '')
    try: P.api('answerCallbackQuery', callback_query_id=q['id'])
    except Exception: pass
    p = data.split(':')
    if p[0] == 'o': return on_onboard(q, chat, mid, uid, p)
    if data == 'm:menu':
        with plock: pending.pop(uid, None)
        return menu(chat, mid)
    if data == 'm:new': return edit(chat, mid, 'What kind of post?', TYPE_ROWS)
    if data == 'm:plan':
        text, rows = plan_view(); return edit(chat, mid, text, rows)
    if data == 'm:teach':
        with plock: pending[uid] = {'mode': 'teach'}
        return edit(chat, mid, 'Teach me. Every message you send now is saved as a rule for new posts.', TEACH_ROWS)
    if data == 'm:pause':
        paused = not is_paused(); set_paused(paused)
        return edit(chat, mid, 'Autopost paused. Posts you scheduled still go out.' if paused else 'Autopost is on again.', menu_rows())
    if p[0] == 'n':
        with plock: pending[uid] = {'mode': 'collect', 'type': p[1], 'notes': [], 'images': [], 'msg': mid}
        return edit(chat, mid, f'{P.LABEL[p[1]]}: add context, images or links, then tap Done.', COLLECT_ROWS)
    if data == 'c:cancel':
        with plock: pending.pop(uid, None)
        return menu(chat, mid)
    if data == 'c:done':
        with plock: st = pending.pop(uid, None)
        if not st: return menu(chat, mid)
        if st['type'] == 'custom' and not st['notes'] and not st['images']:
            with plock: pending[uid] = st
            return edit(chat, mid, 'Custom: send at least one line about the post first.', COLLECT_ROWS)
        unbutton(chat, mid)
        return busy(chat, new_draft, chat, uid, st['type'], '\n'.join(st['notes']), st['images'])
    if p[0] == 'd':
        act, did = p[1], p[2]; d = P.load_draft(did)
        if not d or d.get('status') in ('posted', 'dropped'):
            unbutton(chat, mid); return send(chat, 'That post is already ' + (d or {}).get('status', 'gone') + '.')
        if act == 'post':
            def go():
                mid2 = P.publish_draft(d)
                if d.get('slot'):
                    date, t = slot_of(d); P.update_slot(date, t, draft=None, manual=False, status='planned', type=P.default_slot(date, t)['type'])
                unbutton(chat, mid); send(chat, f'Posted. t.me/pipfarmingresults/{mid2}')
            return busy(chat, go)
        if act == 'chg':
            with plock: pending[uid] = {'mode': 'change', 'draft': did}
            return send(chat, 'What should change? Send one message.')
        if act == 'sch':
            nf = P.next_free_slot(); rows = []
            if nf: rows.append([(f'Next free · {nf[0]:%a} {nf[1]}', f'd:at:{did}:next')])
            rows += [[(t, f'd:at:{did}:{t.replace(":", "")}') for t in P.SLOTS], [('Back', f'd:back:{did}')]]
            return P.api('editMessageReplyMarkup', chat_id=chat, message_id=mid, reply_markup=P.kb(rows))
        if act == 'back': return P.api('editMessageReplyMarkup', chat_id=chat, message_id=mid, reply_markup=P.kb(draft_rows(d)))
        if act == 'at':
            date, t = P.next_free_slot() if p[3] == 'next' else P.next_time(f'{p[3][:2]}:{p[3][2:]}')
            unschedule(d)
            old = P.load_plan(date)[t]; A.clear_previews(old)
            P.update_slot(date, t, draft=did, manual=True, status='planned', type=d['type'], day=d.get('day'), previews=[])
            d = P.load_draft(did); d.update(status='scheduled', slot=slot_key(date, t)); P.save_draft(d)
            P.api('editMessageReplyMarkup', chat_id=chat, message_id=mid, reply_markup=P.kb(draft_rows(d)))
            return send(chat, f"Scheduled for {'today' if date == P.now().date() else f'{date:%a %-d %b}'} {t}.")
        if act == 'uns':
            unschedule(d); d = P.load_draft(did)
            P.api('editMessageReplyMarkup', chat_id=chat, message_id=mid, reply_markup=P.kb(draft_rows(d)))
            return send(chat, 'Unscheduled. The slot goes back to autopost.')
        if act == 'drop':
            unschedule(d); d = P.load_draft(did); d['status'] = 'dropped'; P.save_draft(d); delete(chat, mid)
            return send(chat, 'Dropped.')
    if p[0] == 's':
        act = p[1]; date, t = parse_slot(f'{p[2]}:{p[3]}'); s = P.load_plan(date)[t]
        if act in ('ok', 'skip', 'chg', 'prev', 'swap', 'to') and s['status'] not in ('planned', 'ready'):
            if act != 'prev': unbutton(chat, mid) if act in ('skip', 'chg') else None
            return send(chat, f'{t} is already {STATUS.get(s["status"]) or s["status"]}.')
        if act == 'ok':
            P.update_slot(date, t, approved=True); A.clear_previews(s)
            if P.now() >= P.slot_dt(date, t):
                def go():
                    A.post_slot(date, t); s2 = P.load_plan(date)[t]
                    send(chat, f'Posted. t.me/pipfarmingresults/{s2["posted_msg"]}' if s2['status'] == 'posted' else f'Not posted: {s2.get("error") or s2["status"]}.')
                return busy(chat, go)
            return send(chat, f'Approved. Posts at {t}.')
        if act == 'skip':
            P.update_slot(date, t, status='skipped'); A.clear_previews(s); pending_edit(date, t, skip=True)
            if 'document' in q['message'] or 'photo' in q['message']: unbutton(chat, mid); return send(chat, f'Rejected {t}. Want to make your own?', [[('✍️ Make my own', 'n:custom')]])
            text, rows = plan_view(date); return edit(chat, mid, text, rows)
        if act == 'unskip':
            P.update_slot(date, t, status='planned'); pending_edit(date, t, skip=False); text, rows = plan_view(date); return edit(chat, mid, text, rows)
        if act == 'chg':
            with plock: pending[uid] = {'mode': 'change', 'slot': slot_key(date, t)}
            return send(chat, 'What should change? Send one message.')
        if act == 'prev': return busy(chat, preview_slot, chat, date, t)
        if act == 'swap':
            k = slot_key(date, t)
            rows = [[(P.LABEL[x], f's:to:{k}:{x}') for x in ('results', 'weekly', 'highlight')], [(P.LABEL[x], f's:to:{k}:{x}') for x in ('hype', 'profits')], [('Back', 'm:plan')]]
            return edit(chat, mid, f'{t}: swap {P.slot_label(s)} for…', rows)
        if act == 'to':
            A.clear_previews(s)
            if s.get('draft'):
                d = P.load_draft(s['draft'])
                if d and s.get('manual'): d.update(status='draft', slot=None); P.save_draft(d)
            P.update_slot(date, t, type=p[4], day=None, draft=None, manual=False, status='planned', previews=[])
            text, rows = plan_view(date); return edit(chat, mid, text, rows)
    if p[0] == 'g':
        if data == 'g:show': return send(chat, rules_text(), TEACH_ROWS)
        if data == 'g:clear':
            r = P.guide_rules()
            if not r: return send(chat, 'No rules yet.')
            return send(chat, 'Which rule should go?', [[(f'{i+1}. {x[:40]}', f'g:del:{i}')] for i, x in enumerate(r)] + [[('Back', 'g:done')]])
        if p[1] == 'del':
            P.del_rule(int(p[2])); return edit(chat, mid, f'Rule removed. {len(P.guide_rules())} left.', TEACH_ROWS)
        if data == 'g:done':
            with plock: pending.pop(uid, None)
            return menu(chat, mid)

def unschedule(d):
    if d.get('slot'):
        date, t = slot_of(d); s = P.load_plan(date)[t]
        if s.get('draft') == d['id']:
            A.clear_previews(s)
            P.update_slot(date, t, draft=None, manual=False, status='planned', previews=[], **P.default_slot(date, t))
        d.update(status='draft', slot=None); P.save_draft(d)

# ---------- onboarding: the plan in plain words, then every post format as a test ----------
PLAN_TEXT = ("Here is the plan.\n\n"
             "Every day, 3 posts:\n1 pm  Highlight\n5 pm  Profits\n8 pm  Results\n\n"
             "Every Sunday at 8 pm: the weekly results.\n"
             "First week (23 to 29 Oct): the 9 am post is the launch.\n\n"
             "Nothing goes out until one of you taps Approve.\n\nClear?")
SIMPLE_TEXT = ("Simple version:\n\n"
               "The bot makes 3 posts a day from the live numbers.\n"
               "It sends each one to you first.\n"
               "Approve = it posts. Change = it fixes it. Reject = you make your own.\n\nClear now?")
PLAN_ROWS = [[('✅ Yes, do it', 'o:yes'), ('❓ Not clear', 'o:no')], [('💡 Different plan', 'o:diff')]]
FORMATS = [('launch-day1', 'Launch · day 1'), ('hype-proof', 'Hype · proof'), ('hype-crowd-56', 'Hype · people copying'),
           ('highlight-wr-100', 'Highlight · win rate'), ('highlight-dd-56', 'Highlight · low drawdown'), ('profits-pnl-84', 'Profits · 30 days'),
           ('results-board-roi', 'Results · daily board'), ('weekly-week1', 'Weekly results'), ('custom-example', 'Custom · your own post')]

def onboard(uid):
    send(int(uid), PLAN_TEXT, PLAN_ROWS)

def send_formats(chat):
    send(chat, f"Sending all {len(FORMATS)} post formats now. Test only, old numbers. Nothing goes to the channel.\nApprove the ones you like.")
    for key, label in FORMATS:
        f = os.path.join(P.POSTS, 'preview', key + '.png')
        if not os.path.exists(f): continue
        P.api('sendDocument', files={'document': (f'pipfarming-{key}.png', f)}, chat_id=int(chat), caption=label,
              reply_markup=P.kb([[('✅ Approve', f'o:ok:{key}'), ('❌ Don\'t use', f'o:nope:{key}')]]), http_timeout=120)
    send(chat, 'That is all of them. Approve or skip each one.')

def on_onboard(q, chat, mid, uid, p):
    if p[1] == 'yes': unbutton(chat, mid); return busy(chat, send_formats, chat)
    if p[1] == 'no': return send(chat, SIMPLE_TEXT, PLAN_ROWS)
    if p[1] == 'diff':
        with plock: pending[uid] = {'mode': 'plan_idea'}
        return send(chat, 'Tell me your plan in one message.')
    if p[1] in ('ok', 'nope'):
        votes = P.jread(os.path.join(P.ST, 'format-votes.json'), {}) or {}
        votes.setdefault(p[2], {})[uid] = p[1]; P.jwrite(os.path.join(P.ST, 'format-votes.json'), votes)
        unbutton(chat, mid); return send(chat, ('Approved: ' if p[1] == 'ok' else 'Skipped: ') + dict(FORMATS).get(p[2], p[2]) + '.')

# ---------- assistant chat: talks like a person to the people in PIP_CHAT_IDS (Johannes) ----------
CHAT_IDS = set(__import__('re').findall(r'\d+', P.ENV.get('PIP_CHAT_IDS', '')))
ASSISTANT = """You are Samanway's assistant on Telegram. You run the PIP Farming results channel (@pipfarmingresults) for him.
You are talking to {who} (Samanway is your boss; Johannes is Samanway's boss). Talk like a real person: warm, direct, simple English, short. Never say you are an AI or a bot.
Write 1 to 3 very short messages (each one or two sentences), like texting.
What you know:
- Plan (Johannes decided it): 3 posts a day, 1 pm Highlight, 5 pm Profits, 8 pm Results. Weekly results every Sunday 8 pm. Launch week 23 to 29 Oct adds a 9 am launch post.
- Every post comes to Johannes and Samanway first with Approve, Change, Reject. Nothing goes out without approval. If he's happy he just taps Approve and it goes out as it is.
- All numbers come live from the Levels Socials provider API. Strategies: Cool Wave, PBS FX, Nightshift FX, Glade Coms, Yield Engine, Aurix Pulse, A Little Fox.
- You can make a post for him any time: he says what it should be about, you make it, he can change anything, then approve.
Goals in this chat: confirm the plan is final, find out what else he needs from the automation, and make posts when he asks.
If he asks for a post (or describes one), set action to "make_post" with type (results, highlight, hype, profits, weekly, custom) and notes = what he wants.
If he says something the team must know or change (a new rule, a plan change), set note_for_team to a one-line summary.
Never promise things outside this channel. If you don't know, say you'll check with Samanway."""
CHAT_SCHEMA = {'type': 'object', 'properties': {'messages': {'type': 'array', 'items': {'type': 'string'}}, 'action': {'type': 'string', 'enum': ['none', 'make_post']},
               'type': {'type': 'string'}, 'notes': {'type': 'string'}, 'note_for_team': {'type': 'string'}}, 'required': ['messages', 'action']}

def chat_reply(chat, uid, text):
    hf = os.path.join(P.ST, f'chat-{uid}.json'); hist = P.jread(hf, []) or []
    who = P.allowed().get(uid, 'Teammate').split('_')[0]
    hist.append({'from': who, 'text': text})
    if who == 'Johannes': tell_blip(f'[PIP Farming bot] Johannes wrote to the bot: "{text}". Read it and make any change he asks for in ~/pip-farming (rules in state/guide.md, plan in pipcore.py), then tell Samanway in one line.')
    try: P.api('sendChatAction', chat_id=chat, action='typing')
    except Exception: pass
    convo = '\n'.join(f"{h['from']}: {h['text']}" for h in hist[-16:])
    try: out = P.claude_json(ASSISTANT.replace('{who}', who) + '\n\nConversation so far:\n' + convo + '\n\nReply to his last message.', CHAT_SCHEMA, timeout=90, model='haiku')
    except Exception as e:
        log('chat failed', e); return send(chat, "Give me a second, I'll get back to you on that.")
    for i, msg in enumerate([m for m in out.get('messages', []) if m.strip()][:3]):
        if i: time.sleep(0.8)
        send(chat, msg.strip()); hist.append({'from': 'You', 'text': msg.strip()})
    P.jwrite(hf, hist[-40:])
    if (out.get('note_for_team') or '').strip().lower() not in ('', 'none', 'n/a', 'null', '-'):
        with open(os.path.join(P.ST, 'plan-ideas.md'), 'a') as fh: fh.write(f"- {P.now():%Y-%m-%d %H:%M} {who} (chat): {out['note_for_team']}\n")
        for other in P.allowed():
            if other != uid:
                try: send(int(other), f"{who}: {out['note_for_team']}")
                except Exception: pass
    if out.get('action') == 'make_post':
        kind = out.get('type') if out.get('type') in P.TYPES else 'custom'
        notes = out.get('notes') or text
        busy(chat, new_draft, chat, uid, kind, notes, [])

def tell_blip(text):
    if os.environ.get('PIP_HOME'): return   # tests: never wake the real Blip
    try:
        code = open(os.path.expanduser('~/blip/data/app-access-code')).read().strip()
        req = __import__('urllib.request').request.Request('http://127.0.0.1:8787/api/send', json.dumps({'text': text}).encode(), {'Content-Type': 'application/json', 'Cookie': f'blip={code}'})
        __import__('urllib.request').request.urlopen(req, timeout=10)
    except Exception as e: log('tell_blip failed', e)

def greet(uid):
    msgs = ["Hi Johannes, I'm Samanway's assistant. I run the PIP Farming channel for him.",
            "I've set it up your way: 3 posts a day at 1 pm, 5 pm and 8 pm, plus the weekly results every Sunday at 8 pm.",
            "Every post comes to you first. If you're happy, tap Approve and it goes out as it is. Nothing posts without your OK.",
            "And if you want a post about something specific, just tell me here. I'll make it and you can change anything.",
            "Is this plan final for you, or is there anything else you need from the automation?"]
    hf = os.path.join(P.ST, f'chat-{uid}.json'); hist = P.jread(hf, []) or []
    for m in msgs: send(int(uid), m); hist.append({'from': 'You', 'text': m}); time.sleep(1.2)
    P.jwrite(hf, hist[-40:])

def handle(u):
    m = u.get('message'); q = u.get('callback_query')
    who = (m or q or {}).get('from', {}); chat = (m['chat'] if m else (q or {}).get('message', {}).get('chat', {}))
    if not who or chat.get('type') != 'private': return
    if str(who['id']) not in P.allowed():
        if q:
            try: P.api('answerCallbackQuery', callback_query_id=q['id'])
            except Exception: pass
        return send(chat['id'], f"Your chat ID: {who['id']}. Ask the team to add you.")
    try: on_message(m) if m else on_callback(q)
    except Exception as e:
        log('update failed', repr(e), traceback.format_exc()[-800:])
        try: send(chat['id'], f'That failed: {str(e)[:150]}')
        except Exception: pass

def main():
    me = P.api('getMe'); log('started as @' + me['username'])
    P.refresh_admins(); last_admins = time.time()
    off_f = os.path.join(P.ST, 'offset.json'); offset = (P.jread(off_f, {}) or {}).get('offset')
    if offset is None:   # first start: skip whatever piled up while no bot was listening
        old = P.api('getUpdates', offset=-1, timeout=0); offset = old[-1]['update_id'] + 1 if old else 0
    while True:
        try:
            ups = P.api('getUpdates', offset=offset, timeout=50, allowed_updates=['message', 'callback_query'])
        except Exception as e:
            log('poll failed', e); time.sleep(5); continue
        for u in ups:
            offset = u['update_id'] + 1; P.jwrite(off_f, {'offset': offset})
            handle(u)
        if time.time() - last_admins > 6 * 3600: P.refresh_admins(); last_admins = time.time()

if __name__ == '__main__':
    if sys.argv[1:] == ['greet']:
        for uid in CHAT_IDS: greet(uid); print('greeted', uid)
    elif sys.argv[1:] == ['onboard']:
        for uid in P.allowed(): onboard(uid); print('sent to', P.allowed()[uid])
    else: main()
