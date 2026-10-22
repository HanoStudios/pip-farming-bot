#!/usr/bin/env python3
"""End-to-end test of bot.py + autopost.py against a FAKE Telegram (local HTTP server) in a throwaway copy.
Never touches the real channel, group, state or token. Claude is a stub; rendering is the real Chrome renderer.
usage: python3 tests/test_bot.py        exits 0 when every check passes."""
import os, sys, json, time, shutil, tempfile, threading, subprocess, datetime, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO = os.path.expanduser('~/Desktop/Levels Socials Studio/statics/data')
SB = tempfile.mkdtemp(prefix='pip-test-'); print('sandbox:', SB)
for f in ('pipcore.py', 'autopost.py', 'bot.py'): shutil.copy(os.path.join(SRC, f), SB)
shutil.copytree(os.path.join(SRC, 'brand'), os.path.join(SB, 'brand'))
ADMIN, ADMIN2, STRANGER, CHANNEL = 4242, 777, 999, '-1003897919358'
open(os.path.join(SB, '.env'), 'w').write(f'TELEGRAM_BOT_TOKEN=TEST:TOKEN\nPIP_ALLOWED_IDS=31337\n')
DATA = os.path.join(SB, 'data'); os.makedirs(DATA)
raw = json.load(open(os.path.join(STUDIO, 'providers-api.json')))
def write_data(hours_old):
    raw['data']['asOf'] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_old)).strftime('%Y-%m-%dT%H:%M:%SZ')
    json.dump(raw, open(os.path.join(DATA, 'providers-api.json'), 'w'))
write_data(1)
shutil.copy(os.path.join(STUDIO, 'providers-live.json'), DATA)
STUB = os.path.join(SB, 'claude-stub.py')
open(STUB, 'w').write(r'''#!/usr/bin/env python3
import sys, json, re
prompt = sys.argv[sys.argv.index('-p') + 1]; schema = json.loads(sys.argv[sys.argv.index('--json-schema') + 1])
open(__file__ + '.calls', 'a').write(json.dumps(prompt[-400:]) + '\n')
if 'kicker' in schema['properties']:
    out = {'kicker': 'TEAM NOTE', 'headline': 'A note from the team.', 'body': 'Short and calm.', 'caption': 'A quick word from the team.', 'stats': [{'id': 100, 'metric': 'wr'}]}
    if 'BADNUM' in prompt: out['caption'] = 'We made 12345.6 this week.'
else:
    keys = re.findall(r'^\[([^\]]+)\]', prompt, re.M)
    calm = re.findall(r'^\[([^\]]+)\][^\n]*calmest', prompt, re.M)
    out = {'angle': calm[0] if 'calm one' in prompt and calm else keys[0], 'headline': 'Stub headline.', 'caption': 'Stub caption.'}
    if 'Change requested' in prompt: out['caption'] = 'Changed caption.'
    if 'MOONWORD' in prompt: out['caption'] = 'This is going to the moon.'
print(json.dumps({'type': 'result', 'is_error': False, 'result': json.dumps(out), 'structured_output': out}))
'''); os.chmod(STUB, 0o755)
ENVX = dict(os.environ, PIP_HOME=SB, PIP_DATA_FILE=os.path.join(DATA, 'providers-api.json'), PIP_FETCH='/usr/bin/true', PIP_CLAUDE=STUB, PYTHONUNBUFFERED='1')

# ---------- fake Telegram ----------
updates, sent, uid_n, mid_n = [], [], [1], [5000]
cv = threading.Condition()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):   # file downloads
        self.send_response(200); self.end_headers(); self.wfile.write(open(os.path.join(SRC, 'brand', 'mark.png'), 'rb').read())
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0)); body = self.rfile.read(n); method = self.path.rsplit('/', 1)[-1]
        ct = self.headers.get('Content-Type', '')
        if 'multipart' in ct:
            data = {}
            for part in body.split(b'--' + ct.split('boundary=')[1].encode()):
                m = re.search(rb'name="([^"]+)"(; filename="([^"]+)")?.*?\r\n\r\n(.*)\r\n$', part, re.S)
                if m: data[m.group(1).decode()] = f'[file {m.group(3).decode()} {len(m.group(4))} bytes]' if m.group(2) else m.group(4).decode()
            if 'reply_markup' in data: data['reply_markup'] = json.loads(data['reply_markup'])
        else: data = json.loads(body or b'{}')
        res = True
        if method == 'getUpdates':
            with cv:
                if data.get('offset') == -1: res = []
                else:
                    cv.wait_for(lambda: any(u['update_id'] >= data.get('offset', 0) for u in updates), timeout=min(data.get('timeout', 0), 2))
                    res = [u for u in updates if u['update_id'] >= data.get('offset', 0)]
        elif method == 'getMe': res = {'id': 1, 'username': 'test_bot', 'is_bot': True}
        elif method == 'getChatAdministrators': res = [{'user': {'id': 1, 'is_bot': True}, 'status': 'administrator'}, {'user': {'id': ADMIN, 'is_bot': False, 'first_name': 'Jo'}, 'status': 'creator'}, {'user': {'id': ADMIN2, 'is_bot': False, 'first_name': 'Han'}, 'status': 'administrator'}]
        elif method == 'getFile': res = {'file_id': data['file_id'], 'file_path': 'photos/x.jpg'}
        elif method.startswith('send') or method.startswith('edit') or method == 'deleteMessage':
            mid_n[0] += 1; res = {'message_id': data.get('message_id') or mid_n[0], 'chat': {'id': data.get('chat_id')}}
            with cv: sent.append(dict(data, _m=method, _id=res['message_id'])); cv.notify_all()
            btn = ' '.join(f"[{b['text']}→{b['callback_data']}]" for r in (data.get('reply_markup') or {}).get('inline_keyboard', []) for b in r)
            print(f"  BOT {method} to {data.get('chat_id')}: {str(data.get('text') or data.get('caption') or data.get('document') or data.get('photo') or '')[:110]!r} {btn}")
        out = json.dumps({'ok': True, 'result': res}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(out)
srv = ThreadingHTTPServer(('127.0.0.1', 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
ENVX['TELEGRAM_API'] = f'http://127.0.0.1:{srv.server_port}'

def push(u):
    with cv: uid_n[0] += 1; u['update_id'] = uid_n[0]; updates.append(u); cv.notify_all()
def say(text, who=ADMIN, reply_to=None, photo=False):
    print(f'USER {who}: {text}{" +photo" if photo else ""}')
    m = {'message_id': uid_n[0] + 100, 'chat': {'id': who, 'type': 'private'}, 'from': {'id': who}, 'text': text}
    if reply_to: m['reply_to_message'] = {'message_id': reply_to}
    if photo: m.pop('text'); m['photo'] = [{'file_id': 'p1'}]; m['caption'] = text
    push({'message': m})
def tap(data, msg=1, who=ADMIN, doc=False):
    print(f'USER {who} taps {data}')
    push({'callback_query': {'id': str(uid_n[0]), 'from': {'id': who}, 'data': data, 'message': dict({'message_id': msg, 'chat': {'id': who, 'type': 'private'}}, **({'document': {}} if doc else {}))}})
def wait(pred, what, t=90):
    end = time.time() + t; start = len(sent)
    with cv:
        while time.time() < end:
            for s in sent[start - 0:] if False else sent[wait.mark:]:
                if pred(s): wait.mark = sent.index(s) + 1; return s
            cv.wait(1)
    raise SystemExit(f'FAIL: no {what}')
wait.mark = 0
def btn(s, prefix): return next(b['callback_data'] for r in s['reply_markup']['inline_keyboard'] for b in r if b['callback_data'].startswith(prefix))
checks = []
def ok(cond, what): checks.append((cond, what)); print(('PASS ' if cond else 'FAIL ') + what)
st = lambda *p: os.path.join(SB, 'state', *p)

bot = subprocess.Popen([sys.executable, os.path.join(SB, 'bot.py')], env=ENVX, stdout=open(os.path.join(SB, 'bot.out'), 'w'), stderr=subprocess.STDOUT)
try:
    time.sleep(2)
    say('hi', who=STRANGER); ok('Your chat ID: 999' in wait(lambda s: s.get('chat_id') == STRANGER, 'stranger reply')['text'], 'stranger gets their chat id only')
    say('/start'); m = wait(lambda s: s.get('chat_id') == ADMIN and s['_m'] == 'sendMessage', 'menu'); ok('m:new' in json.dumps(m['reply_markup']), 'menu has New post')
    # New post: Highlight with context + image -> preview document
    tap('m:new', m['_id']); wait(lambda s: 'What kind' in str(s.get('text')), 'type picker')
    tap('n:highlight', m['_id']); wait(lambda s: 'add context' in str(s.get('text')), 'collect prompt')
    say('Focus on the calm one'); say('see this', photo=True)
    ok('1 image' in wait(lambda s: 'Got 2 notes, 1 image' in str(s.get('text')), 'counter')['text'], 'notes and images counted')
    tap('c:done', m['_id'])
    doc = wait(lambda s: s['_m'] == 'sendDocument' and s.get('chat_id') == str(ADMIN), 'draft preview')
    ok(doc['caption'] == 'Stub caption.' and 'pipfarming-' in doc['document'], 'draft preview is a full-quality document with the caption')
    did = btn(doc, 'd:post:').split(':')[2]; d = json.load(open(st('drafts', f'{did}.json')))
    ok(d['angle_key'].startswith('dd:'), 'Claude picked the angle that matches the context (calm = drawdown)')
    ok(os.path.getsize(d['png']) > 100000, 'draft image rendered')
    # Change
    tap(f'd:chg:{did}', doc['_id']); wait(lambda s: 'What should change' in str(s.get('text')), 'change prompt')
    say('make the caption warmer')
    doc2 = wait(lambda s: s['_m'] == 'sendDocument', 'changed preview')
    ok(doc2['caption'] == 'Changed caption.' and json.load(open(st('drafts', f'{did}.json')))['version'] == 2, 'Change makes a new version')
    ok(any(s['_m'] == 'deleteMessage' and s['message_id'] == doc['_id'] for s in sent), 'old preview is replaced (deleted)')
    ok('make the caption warmer' in open(st('guide.md')).read(), 'change note saved to guide.md')
    # reply-to-preview change
    say('shorter please', reply_to=doc2['_id']); doc3 = wait(lambda s: s['_m'] == 'sendDocument', 'reply change'); ok(json.load(open(st('drafts', f'{did}.json')))['version'] == 3, 'replying to a preview also changes it')
    # Schedule -> next free slot
    tap(f'd:sch:{did}', doc3['_id']); kbm = wait(lambda s: s['_m'] == 'editMessageReplyMarkup', 'schedule picker')
    tap(btn(kbm, f'd:at:{did}:next'), doc3['_id']); sch = wait(lambda s: 'Scheduled for' in str(s.get('text')), 'scheduled')
    d = json.load(open(st('drafts', f'{did}.json'))); date, hm = d['slot'].split(':')
    plan = json.load(open(st('plan', f'{date}.json'))); t = f'{hm[:2]}:{hm[2:]}'
    ok(plan[t]['draft'] == did and plan[t]['manual'], f"scheduled into {d['slot']} ({sch['text']})")
    # Today's plan
    tap('m:plan', m['_id']); pv = wait(lambda s: 'Today' in str(s.get('text')) or 'Tomorrow' in str(s.get('text')), 'plan')
    ok('(yours)' in pv['text'] and pv['text'].count(':00') == 4, "Today's plan lists 4 slots and the scheduled post")
    print(pv['text'])
    sw = btn(pv, 's:swap:'); k = ':'.join(sw.split(':')[2:4]); tap(sw, pv['_id']); sp = wait(lambda s: 'swap' in str(s.get('text')), 'swap menu')
    tap(f's:to:{k}:profits', pv['_id']); pv2 = wait(lambda s: 'Profits' in str(s.get('text')) and s['_m'] == 'editMessageText', 'swapped plan')
    ok(True, 'swap changes the slot type')
    sk = btn(pv2, 's:skip:'); tap(sk, pv2['_id']); pv3 = wait(lambda s: 'skipped' in str(s.get('text')), 'skip'); ok(True, 'skip from the plan')
    tap(btn(pv3, 's:unskip:'), pv3['_id']); pv4 = wait(lambda s: s['_m'] == 'editMessageText', 'restore'); ok('s:prev:' in json.dumps(pv4['reply_markup']), 'restore a skipped slot')
    tap(btn(pv4, 's:prev:'), pv4['_id']); spv = wait(lambda s: s['_m'] == 'sendDocument', 'slot preview'); ok('s:skip:' in json.dumps(spv['reply_markup']), 'slot preview has Approve / Change / Reject')
    # Teach
    tap('m:teach', m['_id']); wait(lambda s: 'Teach me' in str(s.get('text')), 'teach')
    say('Never mention followers under 1000.'); r = wait(lambda s: 'Saved as rule' in str(s.get('text')), 'rule saved'); ok(True, r['text'])
    tap('g:show', r['_id']); sh = wait(lambda s: 'Never mention followers' in str(s.get('text')), 'show rules'); ok(True, 'show rules')
    tap('g:clear', sh['_id']); cl = wait(lambda s: 'Which rule' in str(s.get('text')), 'clear menu'); n_before = len([l for l in open(st('guide.md')) if l.startswith('- ')])
    tap('g:del:0', cl['_id']); wait(lambda s: 'Rule removed' in str(s.get('text')), 'rule removed')
    ok(len([l for l in open(st('guide.md')) if l.startswith('- ')]) == n_before - 1, 'clear a rule')
    tap('g:done', cl['_id']); wait(lambda s: 'PIP Farming' in str(s.get('text')), 'menu back')
    # Custom with an invented number -> caption rejected, safe fallback used
    tap('m:new', m['_id']); wait(lambda s: 'What kind' in str(s.get('text')), 'type picker 2'); tap('n:custom', m['_id']); wait(lambda s: 'add context' in str(s.get('text')), 'collect 2')
    say('BADNUM we have news'); tap('c:done', m['_id'])
    is_custom = lambda s: s['_m'] == 'sendDocument' and 'd:post:' in json.dumps(s.get('reply_markup')) and json.load(open(st('drafts', btn(s, 'd:post:').split(':')[2] + '.json')))['type'] == 'custom'
    cd = wait(is_custom, 'custom draft')
    ok('12345' not in cd['caption'], f"invented number rejected (caption: {cd['caption']!r})")
    # Post now -> goes to the (fake) channel
    cid = btn(cd, 'd:post:').split(':')[2]; tap(f'd:post:{cid}', cd['_id']); ph = wait(lambda s: s['_m'] == 'sendPhoto', 'channel post')
    ok(ph['chat_id'] == CHANNEL and ph['caption'] == cd['caption'], 'Post now sends the photo + caption to the channel')
    wait(lambda s: 'Posted.' in str(s.get('text')), 'posted reply')
    tap('d:drop:' + did, doc3['_id']); wait(lambda s: s.get('text') == 'Dropped.', 'drop'); ok(json.load(open(st('plan', f'{date}.json')))[t]['manual'] is False, 'drop frees its slot')
    # Pause
    tap('m:pause', m['_id']); wait(lambda s: 'paused' in str(s.get('text')), 'pause'); ok(os.path.exists(st('paused')), 'pause autopost (state/paused)')
    tap('m:pause', m['_id']); wait(lambda s: 'on again' in str(s.get('text')), 'resume'); ok(not os.path.exists(st('paused')), 'resume autopost')

    # ---------- the schedule (autopost tick) on a fixed day ----------
    day = datetime.datetime.now(ZoneInfo('Asia/Bangkok')).date()
    def tick(hm):
        r = subprocess.run([sys.executable, os.path.join(SB, 'autopost.py'), 'prep' if hm.endswith(':31') or hm.endswith(':30') else 'post'], env=dict(ENVX, PIP_NOW=f'{day}T{hm}:00'), capture_output=True, text=True, timeout=300)
        print('  tick', hm, r.stdout.strip()[-300:], r.stderr.strip()[-300:])
    tick('12:31'); pr = wait(lambda s: s['_m'] == 'sendDocument' and '13:00' in str(s.get('caption')), 'preview 30 min early')
    plan = json.load(open(st('plan', f'{day}.json'))); ok(plan['13:00']['status'] == 'ready' and len(plan['13:00']['previews']) == 3, 'preview sent to all 3 admins 30 min early')
    # Change from the preview (bot), then post on time
    tap(btn(pr, 's:chg:'), pr['_id'], doc=True); wait(lambda s: 'What should change' in str(s.get('text')), 'slot change prompt')
    say('less formal'); wait(lambda s: s['_m'] == 'sendDocument' and 'New version' in str(s.get('caption')), 'slot new version')
    pj = json.load(open(st('pending', '1300.json'))); ok(pj['caption'] == 'Changed caption.' and pj['png'].endswith('-v2.png'), 'Change wrote the new caption + png into state/pending/1300.json')
    nv = [x for x in sent if x['_m'] == 'sendDocument' and 'New version' in str(x.get('caption'))][-1]
    n0 = len([x for x in sent if x['_m'] == 'sendPhoto']); tick('13:00'); time.sleep(1)
    ok(len([x for x in sent if x['_m'] == 'sendPhoto']) == n0, 'not approved: nothing posted at slot time')
    tap(btn(nv, 's:ok:'), nv['_id'], doc=True); post = wait(lambda s: s['_m'] == 'sendPhoto', 'approved post')
    ok(post['chat_id'] == CHANNEL and post['caption'] == 'Changed caption.', 'Approve posts the changed version')
    time.sleep(2); ok(json.load(open(st('plan', f'{day}.json')))['13:00']['status'] == 'posted', 'slot marked posted')
    # Skip from the preview
    tick('16:31'); pr = wait(lambda s: s['_m'] == 'sendDocument' and '17:00' in str(s.get('caption')), 'preview 17:00')
    tap(btn(pr, 's:skip:'), pr['_id'], doc=True); wait(lambda s: 'Rejected 17:00' in str(s.get('text')), 'skipped')
    ok(json.load(open(st('pending', '1700.json')))['skip'] is True, 'Skip wrote skip:true into state/pending/1700.json')
    n = len([s for s in sent if s['_m'] == 'sendPhoto']); tick('17:00'); time.sleep(1)
    ok(len([s for s in sent if s['_m'] == 'sendPhoto']) == n, 'skipped slot is not posted')
    # Stale data -> no post, admins told in one line
    subprocess.run([sys.executable, '-c', 'import pipcore as P, datetime; P.update_slot(datetime.date.fromisoformat("%s"), "20:00", draft=None, manual=False, status="planned", approved=True)' % day], env=ENVX, cwd=SB)
    write_data(40); tick('20:00'); w = wait(lambda s: 'not posted' in str(s.get('text')), 'stale warning')
    ok('h old' in w['text'] and len([s for s in sent if s['_m'] == 'sendPhoto']) == n, f"stale data: not posted, admins told ({w['text']!r})")
    write_data(1)
    # Missed slot (Mac asleep)
    d2 = day + datetime.timedelta(days=1); day = d2; tick('11:35'); time.sleep(1)
    ok(json.load(open(st('plan', f'{day}.json')))['09:00']['status'] == 'missed' and len([s for s in sent if s['_m'] == 'sendPhoto']) == n, 'slot >2 h late marked missed, not posted late')
    # Paused: auto slot not posted
    open(st('paused'), 'w').write('x')
    tick('13:00'); time.sleep(1); ok(len([s for s in sent if s['_m'] == 'sendPhoto']) == n, 'paused: nothing autoposted')
    # Sunday weekly + launch day 1 slot types
    r = subprocess.run([sys.executable, '-c', 'import pipcore as P, datetime, json; c=P.config(); s=datetime.date.fromisoformat(c["launch_start"]); '
                        'sun=next(s+datetime.timedelta(days=i) for i in range(7) if (s+datetime.timedelta(days=i)).weekday()==6); '
                        'print(json.dumps([P.load_plan(s)["09:00"]["type"], P.load_plan(s)["09:00"].get("day"), P.load_plan(sun)["20:00"]["type"], P.load_plan(s+datetime.timedelta(days=7))["09:00"]["type"]]))'],
                       env=ENVX, cwd=SB, capture_output=True, text=True)
    ok(json.loads(r.stdout) == ['launch', 1, 'weekly', 'hype'], f'plan: launch day 1 at 09:00, Sunday 20:00 weekly, hype after launch week ({r.stdout.strip()})')
finally:
    bot.terminate(); srv.shutdown()
print(open(os.path.join(SB, 'bot.out')).read()[-1500:])
bad = [w for c, w in checks if not c]
print(f'\n{len(checks) - len(bad)}/{len(checks)} checks passed' + (f'; FAILED: {bad}' if bad else ''))
if not bad: shutil.rmtree(SB, ignore_errors=True)
sys.exit(1 if bad else 0)
