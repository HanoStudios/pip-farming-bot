#!/usr/bin/env python3
"""PIP Farming schedule (Asia/Bangkok). Slots: 09:00 hype (launch sequence during launch week), 13:00 highlight,
17:00 profits, 20:00 results (Sunday: weekly results).
launchd:
  com.pipfarming.prep  08:30 12:30 16:30 19:30 -> `autopost.py prep`  build the next slot's post, preview it to the admins,
                       and write the hook file state/pending/HHMM.json {slot, date, time, type, png, caption, skip:false}
  com.pipfarming.post  09:00 13:00 17:00 20:00 -> `autopost.py post`  post the slot that is due now
At post time: nothing is posted if state/paused exists, if state/pending/HHMM.json has "skip": true, or if the data is
older than 36 h / the render failed (one line in logs/post.log). An edited "caption" or "png" in the pending file is used.
More than 2 h late (Mac asleep): not posted, marked missed.
  autopost.py HH:MM          post that slot now (same checks)       autopost.py prep HH:MM   prepare that slot now
  autopost.py --dry [--claude]  render every post type, angle, launch day 1-7 and weekly into posts/preview/, send nothing
  tick       prep + post for whatever is due (used by the tests)    init   mark today's past slots done (first install)
post.py stays as the old, independent fallback (post.py results|hype)."""
import sys, os, json, datetime, shutil
import pipcore as P

def say_admins(text):
    P.log(text, f='post.log')
    P.notify_admins(text)

def preview_buttons(date, t):
    k = f'{date}:{t.replace(":", "")}'
    return P.kb([[('✅ Approve', f's:ok:{k}'), ('✏️ Change', f's:chg:{k}')], [('❌ Reject', f's:skip:{k}')]])

def send_preview(chat, d, date, t, note=None):
    cap = f"{d['caption']}\n\n{note or f'{t} · posts only after you approve.'}"
    return P.api('sendDocument', files={'document': (f'pipfarming-{date}-{t.replace(":", "")}.png', d['png'])}, chat_id=int(chat), caption=cap,
                 reply_markup=preview_buttons(date, t), http_timeout=120)['message_id']

def clear_previews(s, text=None):
    for chat, mid in s.get('previews', []):
        try: P.api('editMessageReplyMarkup', chat_id=int(chat), message_id=mid, reply_markup={'inline_keyboard': []})
        except Exception: pass

def build_for_slot(date, t, s):
    d = P.make_draft(s['type'], day=s.get('day'), auto=True, by='schedule')
    return P.update_slot(date, t, draft=d['id']), d

def prepare(date, t):
    s = P.load_plan(date)[t]
    if s['status'] != 'planned': return
    if P.config().get('paused') and not s.get('manual'): return
    try:
        if s.get('draft') and P.load_draft(s['draft']): d = P.load_draft(s['draft'])
        else: s, d = build_for_slot(date, t, s)
    except Exception as e:   # the slot itself retries and reports at post time
        P.log(f'prepare {t} failed: {e}', f='post.log'); return
    previews = []
    for uid in P.allowed():
        try: previews.append([uid, send_preview(uid, d, date, t)])
        except Exception as e: P.log('preview failed for', uid, e)
    P.update_slot(date, t, status='ready', previews=previews)
    P.log(f'prepared {t} {P.slot_label(s)} {d["id"]}', f='post.log')

def publish(date, t):
    with P.locked():
        s = P.load_plan(date)[t]
        if s['status'] not in ('planned', 'ready'): return
        if P.config().get('paused') and not s.get('manual'):
            s = dict(s, status='paused')
        else: s = dict(s, status='posting')
        saved = P.jread(P.plan_path(date), {}); saved.setdefault(t, {}).update(status=s['status']); P.jwrite(P.plan_path(date), saved)
    if s['status'] == 'paused': P.log(f'{t} paused, not posted', f='post.log'); clear_previews(s); return
    label = P.slot_label(s)
    try:
        d = P.load_draft(s['draft']) if s.get('draft') else None
        if not d: s, d = build_for_slot(date, t, s)
        mid = P.publish_draft(d)
        P.update_slot(date, t, status='posted', posted_msg=mid)
        clear_previews(s)
    except Exception as e:
        P.update_slot(date, t, status='failed', error=str(e))
        clear_previews(s)
        say_admins(f'{t} {label} not posted: {e}.')

def tick():
    t0 = P.now(); date = t0.date(); plan = P.load_plan(date)
    for t in P.SLOTS:
        s = plan[t]; at = P.slot_dt(date, t)
        if s['status'] not in ('planned', 'ready'): continue
        if at - datetime.timedelta(minutes=35) <= t0 < at: prep_slot(date, t)
        elif at <= t0 <= at + datetime.timedelta(hours=2): post_slot(date, t)
        elif t0 > at + datetime.timedelta(hours=2) and s['status'] != 'ready':
            P.update_slot(date, t, status='missed'); clear_previews(s)
            if t0 < at + datetime.timedelta(hours=8): say_admins(f'{t} {P.slot_label(s)} missed (the Mac was off or asleep). Not posted.')

# ---------- the pending hook (state/pending/HHMM.json) + state/paused ----------
PENDING = os.path.join(P.ST, 'pending'); os.makedirs(PENDING, exist_ok=True)
PAUSED = os.path.join(P.ST, 'paused')

def pending_path(t): return os.path.join(PENDING, t.replace(':', '') + '.json')

def read_pending(date, t):
    """Today's hook file for slot t (also accepts state/pending/HH:MM.json). None if missing, old or unreadable."""
    for f in (pending_path(t), os.path.join(PENDING, t + '.json')):
        j = P.jread(f)
        if isinstance(j, dict) and j.get('date') == str(date): return j
    return None

def write_pending(date, t, s, d):
    j = {'slot': t, 'date': str(date), 'time': P.slot_dt(date, t).isoformat(), 'type': s['type'], 'label': P.slot_label(s),
         'png': d['png'], 'caption': d['caption'], 'skip': False, 'draft': d['id'], 'data_as_of': d['as_of'], 'status': 'pending',
         'prepared': {'png': d['png'], 'caption': d['caption']}}   # what we wrote; anything different at post time is an edit
    with P.locked(): P.jwrite(pending_path(t), j)
    return j

def mark_pending(date, t, status, **kw):
    j = read_pending(date, t)
    if j:
        j.update(status=status, **kw)
        with P.locked(): P.jwrite(pending_path(t), j)

def prep_slot(date, t):
    if os.path.exists(PAUSED): P.log(f'prep {t}: paused (state/paused), nothing prepared', f='post.log'); return
    prepare(date, t)
    s = P.load_plan(date)[t]; d = P.load_draft(s['draft']) if s.get('draft') else None
    if s['status'] == 'ready' and d and not read_pending(date, t): write_pending(date, t, s, d)

def post_slot(date, t):
    """Checks state/paused and the pending hook, applies edits, then posts through publish()."""
    s = P.load_plan(date)[t]
    if s['status'] not in ('planned', 'ready'): return
    if os.path.exists(PAUSED):
        P.update_slot(date, t, status='paused'); clear_previews(s); mark_pending(date, t, 'paused')
        P.log(f'{t} not posted: paused (state/paused)', f='post.log'); return
    if not (s.get('approved') or s.get('manual')):
        P.log(f'{t} waiting for approval, not posted', f='post.log'); return
    pj = read_pending(date, t)
    if pj and pj.get('skip'):
        P.update_slot(date, t, status='skipped'); clear_previews(s); mark_pending(date, t, 'skipped')
        P.log(f'{t} not posted: skipped in state/pending', f='post.log'); return
    if pj and s.get('draft'):
        d = P.load_draft(s['draft']); prep = pj.get('prepared', {}); edits = {}
        if pj.get('caption') and pj['caption'] != prep.get('caption'): edits['caption'] = ' '.join(str(pj['caption']).split())
        if pj.get('png') and pj['png'] != prep.get('png'):
            if not os.path.isfile(pj['png']):
                P.update_slot(date, t, status='failed', error='edited png missing'); clear_previews(s); mark_pending(date, t, 'failed')
                P.log(f'{t} not posted: edited png missing: {pj["png"]}', f='post.log'); return
            edits['png'] = pj['png']
        if d and edits:
            d.update(edits); P.save_draft(d); P.log(f'{t} using edited {"/".join(edits)} from state/pending', f='post.log')
    publish(date, t)
    s = P.load_plan(date)[t]; mark_pending(date, t, s['status'], **({'error': s['error']} if s['status'] == 'failed' else {}))

def due(t0, t, early=0):
    at = P.slot_dt(t0.date(), t); return at - datetime.timedelta(minutes=early) <= t0 <= at + datetime.timedelta(hours=2)

def cmd_prep(t=None):
    t0 = P.now(); date = t0.date()
    for x in ([t] if t else [x for x in P.SLOTS if P.slot_dt(date, x) - datetime.timedelta(minutes=35) <= t0 < P.slot_dt(date, x)]):
        prep_slot(date, x)

def cmd_post(t=None):
    """No slot: post whatever is due (launchd, also after the Mac wakes). Slots more than 2 h late are marked missed."""
    t0 = P.now(); date = t0.date(); plan = P.load_plan(date)
    for x in ([t] if t else P.SLOTS):
        s = plan[x]; at = P.slot_dt(date, x)
        if s['status'] not in ('planned', 'ready'):
            if t: P.log(f'{x} is already {s["status"]}', f='post.log')
            continue
        if t0 > at + datetime.timedelta(hours=2) and (s.get('approved') or s.get('manual') or s['status'] == 'planned') and s['status'] != 'ready':
            P.update_slot(date, x, status='missed'); clear_previews(s); mark_pending(date, x, 'missed')
            P.log(f'{x} {P.slot_label(s)} missed (more than 2 h late), not posted', f='post.log')
        elif t or at <= t0: post_slot(date, x)

def init():
    t0 = P.now(); P.config()
    for t in P.SLOTS:
        if P.slot_dt(t0.date(), t) <= t0 and P.load_plan(t0.date())[t]['status'] == 'planned':
            P.update_slot(t0.date(), t, status='skipped', note='before launch')

def dry(use_claude=False):
    out = os.path.join(P.POSTS, 'preview'); shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
    data = P.load_data()
    jobs = [(k, a) for k in ('hype', 'highlight', 'profits', 'results') for a in P.angles_for(k, data)]
    jobs += [('launch', P.launch_angle(data, n)) for n in range(1, 8)]
    jobs += [('weekly', P.weekly_angle(data))]
    # layout check for a real week-over-week board: SYNTHETIC previous week (numbers lowered a little), never posted
    fake = {'items': [dict(p, allTimeRoi=p['allTimeRoi'] * 0.97 - 1, totalTrades=int(p['totalTrades'] * 0.95)) for p in data.all[:-1]]}
    jobs += [('weekly', dict(P.weekly_angle(data, old=(fake, data.as_of.date() - datetime.timedelta(days=7))), key='layout-test-synthetic'))]
    ok = P.data_numbers(data, sum((a.get('extra', []) for _, a in jobs), []))
    res = []
    for kind, a in jobs:
        head, cap, extra = a['headline'], a['caption'], {}
        if use_claude:
            a2, head, cap, extra = P.write_copy(kind, [a], data, ok=ok)
        name = f"{kind}{'-day' + a['key'][-1] if kind == 'launch' else ''}-{a['key'].replace(':', '-')}.png" if kind != 'launch' else f"launch-day{a['key'][-1]}.png"
        png = os.path.join(out, name)
        try: P.render(P.page_html(a, data, head, extra), png); res.append(f'ok   {name} | {cap}')
        except Exception as e: res.append(f'FAIL {name}: {e}')
    custom = {'kicker': 'BEHIND THE BOARD', 'body': 'Every number on this channel comes from the provider API, the moment we post it.', 'stats': [{'id': data.ps[0]['id'], 'metric': 'roi'}, {'id': data.ps[1]['id'], 'metric': 'wr'}],
              'image': os.path.join(P.BRAND, 'avatar.png')}
    try: P.render(P.page_html({'key': 'custom', 'layout': 'custom'}, data, 'How we pick what to show.', custom), os.path.join(out, 'custom-example.png')); res.append('ok   custom-example.png')
    except Exception as e: res.append(f'FAIL custom-example.png: {e}')
    for f in os.listdir(out):
        if f.endswith('.html'): os.remove(os.path.join(out, f))
    print('\n'.join(res)); return all(r.startswith('ok') for r in res)

if __name__ == '__main__':
    import re
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    cmd = args[0] if args else ('dry' if '--dry' in sys.argv else 'tick')
    slot = next((a for a in args[1:] if re.fullmatch(r'\d\d:\d\d', a)), None)
    if '--dry' in sys.argv or cmd == 'dry': sys.exit(0 if dry('--claude' in sys.argv) else 1)
    if re.fullmatch(r'\d\d:?\d\d', cmd): cmd, slot = 'post', f'{cmd.replace(":", "")[:2]}:{cmd.replace(":", "")[2:]}'
    if slot and slot not in P.SLOTS: sys.exit(f'unknown slot {slot}; slots: {" ".join(P.SLOTS)}')
    if cmd == 'tick': tick()
    elif cmd == 'init': init()
    elif cmd in ('prep', 'prepare'): cmd_prep(slot)
    elif cmd in ('post', 'publish'): cmd_post(slot)
    else: print(__doc__)
