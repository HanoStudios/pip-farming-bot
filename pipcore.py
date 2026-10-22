"""PIP Farming content engine: live provider data -> post angle -> Claude-written headline + caption -> 1080x1350 @2x image.
Shared by autopost.py (the schedule) and bot.py (the control bot). Every number on a post comes from the provider API;
text that Claude writes is checked and any number it can't trace back to the API is rejected.
Env overrides (used by the tests): PIP_HOME, PIP_DATA_FILE, PIP_FETCH, PIP_CLAUDE, TELEGRAM_API, PIP_NOW."""
import time, os, sys, json, re, html, uuid, time, shutil, fcntl, random, math, itertools, datetime, tempfile, subprocess, urllib.request, urllib.error, contextlib
from zoneinfo import ZoneInfo

D = os.environ.get('PIP_HOME') or os.path.dirname(os.path.abspath(__file__))
ST, POSTS, LOGS, BRAND = (os.path.join(D, x) for x in ('state', 'posts', 'logs', 'brand'))
STUDIO = os.path.join(D, 'data')   # fetch-providers.sh + providers-api.json
FETCH = os.environ.get('PIP_FETCH') or os.path.join(STUDIO, 'fetch-providers.sh')
DATA_FILE = os.environ.get('PIP_DATA_FILE') or os.path.join(STUDIO, 'providers-api.json')
SITE_FILE = os.path.join(os.path.dirname(DATA_FILE), 'providers-live.json')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
CLAUDE = os.environ.get('PIP_CLAUDE') or shutil.which('claude') or os.path.expanduser('~/.local/node/bin/claude')
TG = os.environ.get('TELEGRAM_API') or 'https://api.telegram.org'
CHANNEL, GROUP = '-1003897919358', '-1004472913007'
COMMUNITY = 't.me/pipfarmingcommunity'
PROMOTED = (84, 100, 101, 102, 103)   # the only strategies PIP Farming promotes (same 5 as Instagram); logos in brand/providers/<id>.png
TZ = ZoneInfo('Asia/Bangkok')
SLOTS = ['09:00', '11:00', '13:00', '17:00']   # 11 am = yesterday's results (Samanway 2026-10-23); 9 am off
DEFAULT_TYPE = {'09:00': 'hype', '11:00': 'results', '13:00': 'highlight', '17:00': 'profits'}
TYPES = ['results', 'weekly', 'highlight', 'hype', 'profits', 'custom']
LABEL = {'results': 'Daily results', 'weekly': 'Weekly results', 'highlight': 'Creative', 'hype': 'Hype', 'profits': 'Performance update', 'custom': 'Custom', 'launch': 'Launch'}
MAX_AGE_H = 36
DISCLAIMER = 'Trading involves risk. You may lose your invested capital. Past performance does not guarantee future results.'
for d in (ST, POSTS, LOGS, os.path.join(ST, 'plan'), os.path.join(ST, 'drafts'), os.path.join(ST, 'snapshots'), os.path.join(POSTS, 'drafts')):
    os.makedirs(d, exist_ok=True)

def env():
    e = {}
    for line in open(os.path.join(D, '.env')):
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.strip().split('=', 1); e[k.strip()] = v.strip().strip('"\'')
    return e
ENV = env()
TOKEN = ENV.get('TELEGRAM_BOT_TOKEN', '')

def now():
    return datetime.datetime.fromisoformat(os.environ['PIP_NOW']).replace(tzinfo=TZ) if os.environ.get('PIP_NOW') else datetime.datetime.now(TZ)

def log(*a, f='pip.log'):
    line = f"[{now():%Y-%m-%d %H:%M:%S}] " + ' '.join(map(str, a))
    print(line, flush=True)
    with open(os.path.join(LOGS, f), 'a') as fh: fh.write(line + '\n')

# ---------- state (one lock for every read-modify-write, shared by the bot and the scheduler) ----------
@contextlib.contextmanager
def locked():
    with open(os.path.join(ST, '.lock'), 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(fh, fcntl.LOCK_UN)

def jread(path, default=None):
    try:
        with open(path) as fh: return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError): return default

def jwrite(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh: json.dump(obj, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)

def config():
    c = jread(os.path.join(ST, 'config.json'), {})
    if 'launch_start' not in c:   # launch week = the next 7 mornings
        t = now(); start = t.date() if t.hour < 9 else t.date() + datetime.timedelta(days=1)
        c['launch_start'] = start.isoformat(); c.setdefault('paused', False); jwrite(os.path.join(ST, 'config.json'), c)
    return c

def set_config(**kw):
    with locked():
        c = config(); c.update(kw); jwrite(os.path.join(ST, 'config.json'), c)

# ---------- Telegram ----------
class TGError(Exception): pass

def api(method, files=None, http_timeout=75, **params):
    url = f'{TG}/bot{TOKEN}/{method}'
    if files:
        b = uuid.uuid4().hex; body = b''
        for k, v in params.items():
            if v is None: continue
            v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (fname, path) in files.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\nContent-Type: image/png\r\n\r\n'.encode() + open(path, 'rb').read() + b'\r\n'
        body += f'--{b}--\r\n'.encode(); hdr = {'Content-Type': f'multipart/form-data; boundary={b}'}
    else:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode(); hdr = {'Content-Type': 'application/json'}
    try:
        r = json.load(urllib.request.urlopen(urllib.request.Request(url, body, hdr), timeout=http_timeout))
    except urllib.error.HTTPError as e:
        try: r = json.load(e)
        except Exception: raise TGError(f'{method}: HTTP {e.code}')
    if not r.get('ok'): raise TGError(f"{method}: {r.get('description')}")
    return r['result']

def kb(rows):
    return {'inline_keyboard': [[{'text': t, 'callback_data': d} for t, d in row] for row in rows]}

def download(file_id, dest):
    f = api('getFile', file_id=file_id)
    urllib.request.urlretrieve(f'{TG}/file/bot{TOKEN}/{f["file_path"]}', dest)
    return dest

def refresh_admins():
    """Human admins of the channel and the group (+ PIP_ALLOWED_IDS). Kept in state/admins.json."""
    admins = {}
    for chat in (CHANNEL, GROUP):
        try:
            for a in api('getChatAdministrators', chat_id=chat):
                u = a['user']
                if not u.get('is_bot'): admins[str(u['id'])] = u.get('first_name') or u.get('username') or str(u['id'])
        except Exception as e: log('admins: could not read', chat, e)
    for uid in re.findall(r'-?\d+', ENV.get('PIP_ONLY_IDS', '')):   # listed people who aren't admins: their name from their chat
        if uid not in admins:
            try: c = api('getChat', chat_id=int(uid)); admins[uid] = c.get('first_name') or c.get('username') or uid
            except Exception as e: log('admins: could not read chat', uid, e)
    if admins:
        with locked(): jwrite(os.path.join(ST, 'admins.json'), {'updated': now().isoformat(), 'admins': admins})
    return allowed()

def allowed():
    a = dict(jread(os.path.join(ST, 'admins.json'), {}).get('admins', {}))
    for x in re.findall(r'-?\d+', ENV.get('PIP_ALLOWED_IDS', '')): a.setdefault(x, 'extra')
    only = re.findall(r'-?\d+', ENV.get('PIP_ONLY_IDS', ''))   # set = exactly these people may use the bot and get previews (admins or not)
    return {k: a.get(k, 'Teammate') for k in only} if only else a

def notify_admins(text, **kw):
    sent = []
    for uid in allowed():
        try: sent.append((uid, api('sendMessage', chat_id=int(uid), text=text, **kw)['message_id']))
        except Exception as e: log('notify failed for', uid, e)
    return sent

# ---------- provider data ----------
class Stale(Exception): pass

def load_data(fetch=True):
    """Fresh provider data, active strategies only. Raises Stale if older than 36 h. Saves the day's snapshot."""
    if fetch and os.path.exists(DATA_FILE) and time.time() - os.path.getmtime(DATA_FILE) < 600: fetch = False   # fetched in the last 10 min: reuse
    if fetch:
        r = subprocess.run([FETCH], capture_output=True, text=True, timeout=90)
        if r.returncode != 0: log('provider fetch failed, using the last file:', (r.stderr or r.stdout).strip()[:200])
    raw = jread(DATA_FILE)
    if not raw or not raw.get('data', {}).get('items'): raise Stale('no provider data file')
    d = raw['data']; as_of = datetime.datetime.fromisoformat(d['asOf'].replace('Z', '+00:00'))
    age = (now() - as_of).total_seconds() / 3600
    if age > MAX_AGE_H: raise Stale(f'data is {age:.0f} h old')
    snap = os.path.join(ST, 'snapshots', f'{as_of.astimezone(TZ).date()}.json')
    with locked(): jwrite(snap, raw)
    return Data(d, as_of)

def site_inactive():
    j = jread(SITE_FILE, {}) or {}
    return {p['name'].lower().strip() for p in j.get('providers', []) if re.search('inactive', str(p.get('status', '')), re.I)}

def snapshot_back(as_of, backs):
    """The first saved snapshot found that many days back: (data, date) or None."""
    today = as_of.astimezone(TZ).date()
    for back in backs:
        j = jread(os.path.join(ST, 'snapshots', f'{today - datetime.timedelta(days=back)}.json'))
        if j and j.get('data', {}).get('items'): return j['data'], today - datetime.timedelta(days=back)

def previous_day(as_of): return snapshot_back(as_of, (1, 2, 3))

def yesterday_data():
    """Yesterday's last numbers (saved at 23:55), with yesterday's result vs the day before. Falls back to live data."""
    load_data()   # also saves today's snapshot
    j = jread(os.path.join(ST, 'snapshots', f'{now().date() - datetime.timedelta(days=1)}.json'))
    if not j or not j.get('data', {}).get('items'): return load_data(fetch=False)
    d = Data(j['data'], datetime.datetime.fromisoformat(j['data']['asOf'].replace('Z', '+00:00'))); d.when = 'yesterday'
    return d

class Data:
    def __init__(self, d, as_of):
        off = site_inactive()
        self.as_of = as_of
        # today's result = change in total ROI since yesterday's last numbers; this week = since 7 days ago (when those snapshots exist)
        olds = [(k, {q['id']: q for q in s[0]['items']} if s else {}) for k, s in (('dayRoi', previous_day(as_of)), ('weekRoi', snapshot_back(as_of, (7, 8, 9, 10))))]
        self.all = [dict(p, **{k: round(p['allTimeRoi'] - old[p['id']]['allTimeRoi'], 2) for k, old in olds if p['id'] in old})
                    for p in d['items'] if p['id'] in PROMOTED]
        self.ps = [p for p in self.all if p.get('pnl30d') and p['name'].lower().strip() not in off]   # 0 = not trading
        self.by_id = {p['id']: p for p in self.ps}
        self.has_day = any(p.get('dayRoi') for p in self.ps)
        self.when = 'today'   # 'yesterday' when built from yesterday's last numbers (the 11 am results)
    def label(self): return self.as_of.astimezone(TZ).strftime('%-d %b, %H:%M') + ' Bangkok'

# ---------- number formatting + the number check ----------
def nm(p): return p['name'].replace(' - ', ' ')
def f_roi(p): return f"{'+' if p['allTimeRoi'] >= 0 else ''}{p['allTimeRoi']:,.2f}%"
def f_wr(p): return f"{p['winRate']:.2f}%"
def f_dd(p): return f"-{abs(p['drawdown']):.2f}%"
def f_day(p): return f"{'+' if p['dayRoi'] >= 0 else ''}{p['dayRoi']:,.2f}%"
def f_week(p): return f"{'+' if p['weekRoi'] >= 0 else ''}{p['weekRoi']:,.2f}%"
def f_pct(v): return f"{'+' if v >= 0 else ''}{v:,.2f}%"
def logo(p, cls='plogo'): return f'<img class="{cls}" src="file://{BRAND}/providers/{p["id"]}.png" alt="{E(nm(p))}">'

NUM = re.compile(r'\d[\d,]*(?:\.\d+)?')
def canon(s):
    try: return f'{float(str(s).replace(",", "")):.2f}'
    except ValueError: return None

def allowed_numbers(values):
    """Each raw API value is allowed as written, rounded to 1 decimal and to a whole number."""
    out = {canon(x) for x in (*range(1, 11), 30, 100)}   # small counts (5 strategies, number 2, 7 days)
    for v in values:
        if isinstance(v, str):
            out |= {canon(n) for n in NUM.findall(v)}
        else:
            for r in (2, 1, 0): out.add(canon(round(abs(v), r)))
    return out

def bad_numbers(text, ok):
    return [n for n in NUM.findall(text or '') if canon(n) not in ok]

def data_numbers(data, extra=()):
    vals = []
    for p in data.ps:
        vals += [p['allTimeRoi'], p['winRate'], p['drawdown']] + [p[k] for k in ('dayRoi', 'weekRoi') if k in p]
    vals += [len(data.ps), *extra]
    return allowed_numbers(vals)

def not_percent(text):
    """Results are shown in percent only: any number not followed by % (small counts like '5 strategies' aside)."""
    return [n for n in re.findall(r'\d[\d,]*(?:\.\d+)?(?![\d.,]*\s*%)', text or '') if not (n.isdigit() and int(n) <= 10)]

BANNED = re.compile(r'\b(insane|moon\w*|guarantee\w*|risk[- ]free|passive income|lambo|skyrocket\w*|explod\w*|10x|100x|get rich|to the moon|crazy|unreal|massive|huge gains|no[- ]brainer|easy money)\b', re.I)
# Team, 2026-10-22: never say where the numbers come from, that we post daily, "no edits", or any date, time or place
NEVER = re.compile(r"\b(api|provider\w*|data|source\w*|every ?day|no edits?|nothing edited|unedited|edit\w*|bangkok|thailand|tonight|o'?clock"
                   r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|june|july|august|september|october|november|december"
                   r"|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\b|\b\d{1,2}(:\d\d|\s?[ap]\.?m\b)", re.I)
EMOJI = re.compile('[\U0001F000-\U0001FAFF☀-➿⬀-⯿]')

def text_problems(text, ok, what):
    probs = []
    if NEVER.search(text or ''): probs.append(f'{what}: never mention "{NEVER.search(text).group(0)}" (no sources, no "daily", no dates, times or places)')
    bn = bad_numbers(text, ok)
    if bn: probs.append(f'{what}: numbers not in the data: ' + ', '.join(bn))
    np = not_percent(text)
    if np: probs.append(f'{what}: results only in percent, not: ' + ', '.join(np))
    return probs

def schedule_problems(text, ok):
    """For the schedule's own captions (several lines, emoji allowed): no banned words, every number from the data."""
    return ([f'never mention "{NEVER.search(text).group(0)}"'] if NEVER.search(text or '') else []) + \
           ([f'numbers not in the data: {", ".join(bad_numbers(text, ok))}'] if bad_numbers(text, ok) else [])

def caption_problems(cap, ok):
    probs = []
    if not cap or not cap.strip(): return ['empty']
    if '\n' in cap.strip(): probs.append('must be one line')
    if len(cap) > 140: probs.append('too long (max 140 characters)')
    if BANNED.search(cap): probs.append(f'banned word "{BANNED.search(cap).group(0)}"')
    if len(EMOJI.findall(cap)) > 1: probs.append('more than one emoji')
    if '#' in cap: probs.append('no hashtags')
    if re.search(r'\b[A-Z]{5,}\b', cap.replace('PIP', '')): probs.append('no all-caps words')
    return probs + text_problems(cap, ok, 'caption')

# ---------- rotation (so posts don't repeat) ----------
def recent(kind, n):
    return jread(os.path.join(ST, 'rotation.json'), {}).get(kind, [])[-n:]

def remember(kind, key):
    with locked():
        r = jread(os.path.join(ST, 'rotation.json'), {}); r[kind] = (r.get(kind, []) + [key])[-30:]
        jwrite(os.path.join(ST, 'rotation.json'), r)

def pick(kind, keys, avoid):
    used = recent(kind, avoid)
    return next((k for k in keys if k not in used), keys[0])

# ---------- angles: what a post is about (all numbers filled in by code) ----------
def facts(p):
    return f"total ROI {f_roi(p)}, win rate {f_wr(p)}, max drawdown {f_dd(p)}" + (f", today {f_day(p)}" if 'dayRoi' in p else '')

def stat_angle(key, p, kicker, metric, head, cap, extra_brief=''):
    return {'key': key, 'layout': 'stat', 'kicker': kicker, 'metric': metric, 'big': METRIC[metric][1](p), 'pid': p['id'], 'headline': head, 'caption': cap,
            'brief': f"{kicker.title()} spotlight on {nm(p)} ({p['strategyType']}): {facts(p)}. {extra_brief}"}

def day_up(data):
    """Strategies that are up today, best first (empty without yesterday's numbers)."""
    return sorted([p for p in data.ps if p.get('dayRoi', 0) > 0], key=lambda p: -p['dayRoi'])

BOARD_SORTS = {'day': ("TODAY'S RESULT", lambda p: -p.get('dayRoi', -1e9)), 'roi': ('TOTAL ROI', lambda p: -p['allTimeRoi']),
               'wr': ('WIN RATE', lambda p: -p['winRate']), 'dd': ('MAX DRAWDOWN', lambda p: p['drawdown'])}
def board_angle(data, sort, key=None, head='The numbers. Nothing else.', cap="Today's board. Which one would you copy?", kicker=None, blur=False):
    lab, f = BOARD_SORTS[sort]; ps = sorted(data.ps, key=f)
    top = ps[0]
    return {'key': key or f'board:{sort}', 'layout': 'board', 'sort': sort, 'kicker': kicker or f'STRATEGY INDEX · BY {lab}', 'rows': [p['id'] for p in ps],
            'headline': head, 'caption': cap, 'blur': blur,
            'brief': f'The full board of {len(data.ps)} strategies sorted by {lab.lower()}. Leader: {nm(top)}. Rows: ' + '; '.join(f"{nm(p)}: {facts(p)}" for p in ps)}

def styles_angle(data, key='styles', head=None, cap='Automated, algo or manual. Pick the style that fits you.'):
    kinds = {}
    for p in data.ps: kinds[p['strategyType']] = kinds.get(p['strategyType'], 0) + 1
    head = head or f'{len(data.ps)} strategies. {len(kinds)} styles.'
    return {'key': key, 'layout': 'styles', 'kicker': 'THE LINEUP', 'headline': head, 'caption': cap,
            'brief': f'Meet the lineup: {len(data.ps)} strategies in {len(kinds)} styles (' + ', '.join(f'{v} {k}' for k, v in kinds.items()) + '): ' +
                     ', '.join(f"{nm(p)} ({p['strategyType']})" for p in data.ps)}

def community_angle(data, key='community', head='Ask the people who copy them.', cap=f'Questions about a strategy? Ask the community: {COMMUNITY}'):
    return {'key': key, 'layout': 'community', 'kicker': 'COMMUNITY', 'headline': head, 'caption': cap,
            'brief': f'Invite to the PipFarming Community group ({COMMUNITY}): talk about the strategies and the daily results with people who copy them. The caption must contain {COMMUNITY}.'}

def hype_angles(data):
    return [styles_angle(data),
            board_angle(data, 'wr', key='blur', head='Which one would you copy?', cap='Same board, numbers hidden. The full board drops later.', kicker='GUESS THE LEADER', blur=True),
            community_angle(data)]

def launch_angle(data, day):
    if day == 1:
        a = board_angle(data, 'roi', key='launch1', head="Something's live.", cap="Something's live. The numbers start tomorrow.", kicker='PIP FARMING RESULTS', blur=True)
        a['brief'] = f'Launch day 1 teaser: the channel is live. {len(data.ps)} copy-trading strategies, real results. The image shows the board with the numbers blurred. Classy, curious, short.'
        a['layout'] = 'teaser'; return a
    if day == 2: return styles_angle(data, key='launch2', cap='Meet the lineup. One strategy at a time this week.')
    if day == 3:
        p = max(data.ps, key=lambda p: p['allTimeRoi'])
        return stat_angle('launch3', p, 'STRATEGY REVEAL', 'roi', f'First reveal: {nm(p)}.', f'First reveal: {nm(p)}. Total ROI, all time.', 'Launch week reveal 1: the highest total ROI. The big number is total ROI.')
    if day == 4:
        p = max(data.ps, key=lambda p: p['winRate'])
        return stat_angle('launch4', p, 'STRATEGY REVEAL', 'wr', f'{nm(p)} wins most trades.', f'{nm(p)} closes {f_wr(p)} of its trades in profit.', 'Launch week reveal 2: the best win rate. The big number is win rate.')
    if day == 5:
        p = min(data.ps, key=lambda p: p['drawdown'])
        return stat_angle('launch5', p, 'STRATEGY REVEAL', 'dd', f'The calm one: {nm(p)}.', f'Not every strategy needs to be loud. {nm(p)}.', 'Launch week reveal 3: the lowest max drawdown. The big number is max drawdown.')
    if day == 6: return community_angle(data, key='launch6')
    return board_angle(data, 'roi', key='launch7', head='One week in.', cap='One week in. Here is the full board.', kicker='LAUNCH WEEK · DAY 7')

# ---------- number-first posts (team, 2026-10-22: like Akhil's templates; the title is a label, the numbers are the post) ----------
FOLLOW = 'Daily Copy Trading Results'
def top3(ps, f): return ', '.join(f'{nm(p)} {f(p)}' for p in ps[:3])
def cell(text, cls='', pill=False): return [text, cls, pill]
def sign(v): return 'g' if v >= 0 else 'r'
NFA = '\n\nNot financial advice.'
def lines(ps, f, key):   # one line per strategy, green/red dot
    dot = lambda v: '🟢' if v > 0 else '🔴' if v < 0 else '⚪'
    return '\n'.join(f'{dot(p[key])} {nm(p)} {f(p)}' if key in p else f'⚪ {nm(p)} –' for p in ps)

def spot_angles(data):
    """Performance update on one strategy (Akhil's stat grid). Strategies up today first; without a day result, all of them by total ROI."""
    ps = day_up(data) or sorted(data.ps, key=lambda p: -p['allTimeRoi'])
    out = []
    for p in ps:
        cap = (f'{nm(p)} today 📈\n\n{f_day(p)} on the day. Win rate {f_wr(p)}, total ROI {f_roi(p)}.' if p.get('dayRoi', 0) > 0 else
               f'{nm(p)} 📊\n\nTotal ROI {f_roi(p)}, win rate {f_wr(p)}, max drawdown {f_dd(p)}.') + NFA
        calm = ' Lowest max drawdown of the five (calmest).' if p is min(data.ps, key=lambda q: q['drawdown']) else ''
        out.append({'key': f'spot:{p["id"]}', 'layout': 'spot', 'pid': p['id'], 'metric': 'day' if 'dayRoi' in p else 'roi', 'headline': nm(p), 'caption': cap,
                    'brief': f'Performance update on {nm(p)} ({p["strategyType"]}): {facts(p)}.{calm}'})
    return out

def table_angle(data, key, title, chip, cols, rows, cards, cap):
    return {'key': key, 'layout': 'table', 'title': title, 'chip': chip, 'cols': cols, 'rows': rows, 'cards': cards, 'headline': title, 'caption': cap,
            'brief': f'{title}. ' + '; '.join(f'{nm(data.by_id[r["id"]])}: {facts(data.by_id[r["id"]])}' for r in rows)}

def daily_angle(data):
    """Daily Results: every strategy's result of the day."""
    ps = sorted(data.ps, key=lambda p: -p.get('dayRoi', -1e9)); up = day_up(data)
    avg = round(sum(p.get('dayRoi', 0) for p in ps) / len(ps), 2)
    rows = [{'id': p['id'], 'cells': [cell(f_day(p), sign(p['dayRoi']), True) if 'dayRoi' in p else cell('–'), cell(f_wr(p)), cell(f_roi(p), 'g')]} for p in ps]
    w = data.when; W = w.capitalize()
    cards = [['trophy', f'Best {w}', f_day(ps[0]), nm(ps[0]), 'g'], ['chart', f'In profit {w}', f'{len(up)} of {len(ps)}', 'strategies', ''],
             ['pct', f'Average {w}', f_pct(avg), 'all strategies', sign(avg)]]
    a = table_angle(data, 'daily', 'Daily Results', f"{W}'s results", [w.upper(), 'WIN RATE', 'TOTAL ROI'], rows, cards,
                    f"{W}'s results 📊\n\n" + lines(ps, f_day, 'dayRoi') + NFA)
    a['extra'] = [avg]; return a

def index_angle(data, title='Strategy Index', chip='All-time results', key='index'):
    """All-time board (until there is a day result): total ROI, win rate, max drawdown."""
    ps = sorted(data.ps, key=lambda p: -p['allTimeRoi'])
    rows = [{'id': p['id'], 'cells': [cell(f_roi(p), 'g', True), cell(f_wr(p)), cell(f_dd(p), 'r')]} for p in ps]
    wr = max(ps, key=lambda p: p['winRate']); dd = min(ps, key=lambda p: p['drawdown'])
    cards = [['trophy', 'Highest ROI', f_roi(ps[0]), nm(ps[0]), 'g'], ['chart', 'Best win rate', f_wr(wr), nm(wr), ''], ['pct', 'Lowest drawdown', f_dd(dd), nm(dd), 'r']]
    return table_angle(data, key, title, chip, ['TOTAL ROI', 'WIN RATE', 'MAX DD'], rows, cards, f'{title} 📊\n\nTotal ROI, all time:\n' + lines(ps, f_roi, 'allTimeRoi') + NFA)

def results_angles(data):
    return [daily_angle(data) if data.has_day else index_angle(data)]

def weekly_angle(data, old=None):
    """Weekly Results: change in total ROI over the last 7 days. Without a snapshot 7+ days old: the all-time board, titled Weekly Results."""
    if old is None: old = snapshot_back(data.as_of, (7, 8, 9, 10))
    if not old: return index_angle(data, title='Weekly Results', chip='Week 1', key='week1')
    prev = {p['id']: p for p in old[0]['items']}
    wk = {p['id']: round(p['allTimeRoi'] - prev[p['id']]['allTimeRoi'], 2) for p in data.ps if p['id'] in prev}
    ps = sorted(data.ps, key=lambda p: -wk.get(p['id'], -1e9)); up = [p for p in ps if wk.get(p['id'], 0) > 0]
    avg = round(sum(wk.values()) / max(len(wk), 1), 2)
    rows = [{'id': p['id'], 'cells': [cell(f_pct(wk[p['id']]), sign(wk[p['id']]), True) if p['id'] in wk else cell('New'), cell(f_wr(p)), cell(f_roi(p), 'g')]} for p in ps]
    cards = [['trophy', 'Best this week', f_pct(wk[ps[0]['id']]) if ps[0]['id'] in wk else 'New', nm(ps[0]), 'g'],
             ['chart', 'In profit this week', f'{len(up)} of {len(ps)}', 'strategies', ''], ['pct', 'Average this week', f_pct(avg), 'all strategies', sign(avg)]]
    a = table_angle(data, 'weekly', 'Weekly Results', 'This week', ['THIS WEEK', 'WIN RATE', 'TOTAL ROI'], rows, cards,
                    "This week's results 📊\n\n" + lines([dict(p, wk=wk[p['id']]) if p['id'] in wk else p for p in ps], lambda p: f_pct(p['wk']), 'wk') + NFA)
    a.update(extra=list(wk.values()) + [avg], follow='Weekly Copy Trading Results'); return a


# ---------- 1 pm creative post (team, 2026-10-22): on fire / the podium / 93 out of 100, in rotation ----------
CREATIVE = ('fire', 'race', 'grid', 'vs', 'podium', 'gauge', 'cards', 'club', 'ticker', 'guess')   # 1 pm rotation: a different format every day
SPIRIT = {84: '⚔️', 100: '🌊', 101: '🛡️', 102: '⚙️', 103: '🌙'}
def strong_day(data): up = day_up(data); return bool(up) and up[0]['dayRoi'] >= 0.2   # a real move, not +0.04% just after midnight

def lead(data):
    """What the creative posts rank by: today's result when someone is up today, else all-time ROI. Returns (metric, value fn)."""
    return ('day', lambda p: p.get('dayRoi', -1e9)) if strong_day(data) else ('roi', lambda p: p['allTimeRoi'])

def creative_angles(data):
    up = day_up(data); v = now().timetuple().tm_yday % 2; out = []
    if strong_day(data):   # "on fire" only for a real move; otherwise the all-time version
        p = up[0]
        cap = [f'{nm(p)} is on fire today 🔥\n\n{f_day(p)} and top of the board.', f'🔥 Best of the day: {nm(p)}\n\n{f_day(p)} today, {f_wr(p)} win rate.'][v]
        out.append({'key': f'fire:{p["id"]}', 'layout': 'fire', 'pid': p['id'], 'metric': 'day', 'headline': nm(p), 'caption': cap + NFA})
    else:
        p = max(data.ps, key=lambda p: p['allTimeRoi'])
        out.append({'key': f'fire:{p["id"]}', 'layout': 'fire', 'pid': p['id'], 'metric': 'roi', 'headline': nm(p),
                    'caption': f'{nm(p)} 🔥🔥🔥\n\n{f_roi(p)} total ROI. {p["strategyType"]}, start to now.' + NFA})
    if len(up) >= 3:
        ps, m = up[:3], 'day'; cap = "Today's podium 🏆\n\n" + '\n'.join(f'{md} {nm(p)} {f_day(p)}' for md, p in zip('🥇🥈🥉', ps)) + "\n\nWho's your pick? Drop it in the comments 💬"
    else:
        ps, m = sorted(data.ps, key=lambda p: -p['allTimeRoi'])[:3], 'roi'
        cap = "Who's your pick? 👇\n\n" + '\n'.join(f'{md} {nm(p)} {f_roi(p)}' for md, p in zip('🥇🥈🥉', ps)) + '\n\nTotal ROI, all time. Drop your pick in the comments 💬'
    out.append({'key': f'podium:{m}', 'layout': 'podium', 'rows': [p['id'] for p in ps], 'metric': m, 'headline': "Who's your pick?", 'caption': cap + NFA})
    for p in sorted(data.ps, key=lambda p: -p['winRate']):
        out.append({'key': f'grid:{p["id"]}', 'layout': 'grid', 'pid': p['id'], 'metric': 'wr', 'headline': nm(p),
                    'caption': f'{SPIRIT.get(p["id"], "📊")} {nm(p)}\n\nOut of every 100 trades, {round(p["winRate"])} close in profit.\n\n{f_wr(p)} win rate.' + NFA})
    m, v = lead(data); ps = sorted([p for p in data.ps if m != 'day' or 'dayRoi' in p], key=lambda p: -v(p)); val = METRIC[m][1]
    out.append({'key': f'race:{m}', 'layout': 'race', 'rows': [p['id'] for p in ps], 'metric': m, 'headline': 'The race',
                'caption': ("Today's race 🏁" if m == 'day' else 'The race to the top 🏁') + '\n\n' + '\n'.join(f'{i}. {nm(p)} {val(p)}' for i, p in enumerate(ps, 1)) + '\n\nWho takes it next? 👀' + NFA})
    for x, y in itertools.combinations(ps, 2):
        out.append({'key': f'vs:{x["id"]}-{y["id"]}', 'layout': 'vs', 'rows': [x['id'], y['id']], 'metric': m, 'headline': f'{nm(x)} vs {nm(y)}',
                    'caption': f'{nm(x)} vs {nm(y)} 🥊\n\n' + '\n'.join(f'{nm(p)}: {f_roi(p)} total ROI, {f_wr(p)} win rate' for p in (x, y)) + "\n\nWho's your pick? 👇" + NFA})
    p = min(data.ps, key=lambda p: p['drawdown'])
    out.append({'key': f'gauge:{p["id"]}', 'layout': 'gauge', 'pid': p['id'], 'metric': 'dd', 'headline': 'The calm one',
                'caption': f"The calm one 🛡️\n\n{nm(p)}'s deepest dip, all time: {f_dd(p)}.\nWin rate {f_wr(p)}, total ROI {f_roi(p)}." + NFA})
    out.append({'key': f'cards:{m}', 'layout': 'cards', 'rows': [p['id'] for p in ps], 'metric': m, 'headline': 'Pick your card',
                'caption': 'Pick your card 🃏\n\n' + '\n'.join(f'{md} {nm(p)} {val(p)}' for md, p in zip(['🥇', '🥈', '🥉', '4️⃣', '5️⃣'], ps)) + '\n\nWhich one are you copying? 👇' + NFA})
    for p in sorted(data.ps, key=lambda p: -p['allTimeRoi']):
        club = int(p['allTimeRoi'] // 100 * 100)
        if club >= 100:
            out.append({'key': f'club:{p["id"]}:{club}', 'layout': 'club', 'pid': p['id'], 'club': club, 'metric': 'roi', 'headline': f'+{club}% club', 'extra': [club],
                        'caption': f'{nm(p)} is in the +{club}% club 🎉\n\n{f_roi(p)} total ROI, all time.' + NFA})
    bo = sorted(data.ps, key=lambda p: -p['allTimeRoi'])
    out.append({'key': 'ticker:roi', 'layout': 'ticker', 'rows': [p['id'] for p in bo], 'metric': 'roi', 'headline': 'The board',
                'caption': 'The board 📟\n\n' + '\n'.join(f'▲ {nm(p)} {f_roi(p)} · win rate {f_wr(p)}' for p in bo) + NFA})
    for p in sorted(data.ps, key=lambda p: -p['winRate']):
        out.append({'key': f'guess:{p["id"]}', 'layout': 'guess', 'pid': p['id'], 'metric': 'roi', 'headline': 'Guess who?',
                    'caption': f'Guess who? 🤔\n\nTotal ROI {f_roi(p)}, win rate {f_wr(p)}, max drawdown {f_dd(p)}. {p["strategyType"]}.\n\nDrop your guess in the comments 👇' + NFA})
    for a in out: a['brief'] = f"Creative post ({a['layout']}): " + '; '.join(f'{nm(data.by_id[i])}: {facts(data.by_id[i])}' for i in a.get('rows') or [a['pid']])
    return out

def angles_for(kind, data, day=None):
    return {'highlight': creative_angles, 'profits': spot_angles, 'results': results_angles, 'hype': hype_angles,
            'weekly': lambda d: [weekly_angle(d)], 'launch': lambda d: [launch_angle(d, day)]}[kind](data)

def auto_angle(kind, data, day=None):
    angs = angles_for(kind, data, day)
    if kind == 'highlight':   # the next format in the rotation; the win-rate grid moves on to the next strategy each time
        last = (recent('highlight', 1) or ['grid'])[0].split(':')[0]
        fmt = CREATIVE[(CREATIVE.index(last) + 1) % len(CREATIVE)] if last in CREATIVE else CREATIVE[0]
        cands = [a for a in angs if a['layout'] == fmt]; used = recent('highlight', 15)
        return next((a for a in cands if a['key'] not in used), cands[0])
    if kind == 'profits':   # performance update: a different strategy each time (up today first)
        used = recent('profits', min(3, len(angs) - 1)); return next((a for a in angs if a['key'] not in used), angs[0])
    if kind == 'hype':
        used = [k.split(':')[0] for k in recent(kind, len(angs) - 1)]
        return next((a for a in angs if a['key'].split(':')[0] not in used), angs[0])
    return angs[0]

# ---------- copy: Claude writes the headline + caption, the code checks them ----------
STYLE = """You write for the PIP Farming Results Telegram channel. It shows the results of 5 copy-trading strategies: Cool Wave, Glade Coms, Nightshift FX, PBS FX, Yield Engine.
Caption rules: ONE short line (max ~70 characters), sounds like a person texting a friend, not a report. Max one number. Simple, calm, confident. Talk about the results: who did well, by how much. Use psychology quietly: curiosity, proof.
Never mention: the API, the provider, data, sources, "straight from", "live from", "posted every day", "daily", "no edits", "nothing edited", any date, weekday, time of day or place (no "Bangkok", no "20:00", no "tonight at"). No rankings like "2 of 5", no strategy types in brackets.
No hype words (insane, moon, guaranteed, risk-free, passive income, crazy, massive, easy money). No hashtags, no all caps, at most one emoji (prefer none).
When the facts include today's result, the post is about today's result (who did best today, by how much); total ROI is background at most.
Only say good things about a strategy: never point out who lost, dipped deepest or loses more often.
Vary the wording from post to post; no filler like "quietly", "honestly", "worth a look", "worth knowing", "sitting at", "doing its thing", "keeps building".
Never promise future profit. Numbers: results in percent only (never trade counts, follower counts or money), and only numbers given in the facts, copied exactly as written with their % sign.
Headline (goes on the image): max 7 words, sentence case, calm, ends with a full stop. Same rules. It should not repeat the caption."""

def guide_rules():
    f = os.path.join(ST, 'guide.md')
    if not os.path.exists(f): return []
    return [l[2:].strip() for l in open(f) if l.startswith('- ')]

def claude_json(prompt, schema, images=(), timeout=180, model='sonnet'):
    cmd = [CLAUDE, '-p', prompt, '--output-format', 'json', '--model', model, '--no-session-persistence', '--strict-mcp-config',
           '--json-schema', json.dumps(schema)]
    if images: cmd += ['--tools', 'Read', '--allowedTools', 'Read'] + sum((['--add-dir', os.path.dirname(i)] for i in images[:1]), [])
    else: cmd += ['--tools', '']
    child = {k: v for k, v in os.environ.items() if not k.startswith('CLAUDE')}
    child['PATH'] = os.path.dirname(CLAUDE) + ':/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:' + child.get('PATH', '')
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=child, cwd=tempfile.gettempdir())
    try: out = json.loads(r.stdout)
    except json.JSONDecodeError: raise RuntimeError(f'claude: {(r.stderr or r.stdout)[:200]}')
    if out.get('is_error'): raise RuntimeError(f"claude: {str(out.get('result'))[:200]}")
    so = out.get('structured_output')
    if so is None:
        m = re.search(r'\{.*\}', out.get('result', ''), re.S); so = json.loads(m.group(0)) if m else None
    if not isinstance(so, dict): raise RuntimeError('claude: no JSON')
    return so

COPY_SCHEMA = {'type': 'object', 'properties': {'angle': {'type': 'string'}, 'headline': {'type': 'string'}, 'caption': {'type': 'string'}}, 'required': ['angle', 'headline', 'caption']}
CUSTOM_SCHEMA = {'type': 'object', 'properties': {
    'kicker': {'type': 'string'}, 'headline': {'type': 'string'}, 'body': {'type': 'string'}, 'caption': {'type': 'string'},
    'stats': {'type': 'array', 'items': {'type': 'object', 'properties': {'id': {'type': 'integer'}, 'metric': {'type': 'string', 'enum': ['roi', 'wr', 'dd', 'day']}}, 'required': ['id', 'metric']}}},
    'required': ['kicker', 'headline', 'body', 'caption', 'stats']}

def write_copy(kind, angles, data, context='', images=(), prev=None, note='', ok=None):
    """Returns (angle, headline, caption, extra) — Claude's text if it passes the checks, else the angle's own safe text."""
    if kind != 'custom' and angles and not (context or images or prev):   # the schedule: numbers only, no AI copy (team, 2026-10-22)
        return angles[0], angles[0]['headline'], angles[0]['caption'], {}
    rules = guide_rules(); caps = recent('captions', 12)
    base = STYLE + ('\nTeam rules (always follow):\n' + '\n'.join(f'- {r}' for r in rules) if rules else '') + \
        ('\nRecent captions (do not repeat their wording):\n' + '\n'.join(f'- {c}' for c in caps) if caps else '')
    if kind == 'custom':
        lines = '\n'.join(f"id {p['id']}: {nm(p)} ({p['strategyType']}) {facts(p)}" for p in data.ps)
        task = (f"Make a custom post from the team's notes.\nTeam notes:\n{context or '(none)'}\n" + (f"Images from the team (look at them with Read): {', '.join(images)}\n" if images else '') +
                f"The strategies (the only numbers you may use):\n{lines}\n"
                "Write: kicker (2-4 word uppercase label), headline (max 7 words), body (one or two short sentences, max 150 characters, no numbers unless from the data), "
                "caption (one line), stats (0-3 numbers to show as cards, as {id, metric}; use none if they don't fit the message).")
        schema = CUSTOM_SCHEMA
    else:
        opts = '\n'.join(f"[{a['key']}] {a['brief']} (example headline: {a['headline']})" for a in angles)
        task = (f"Post type: {LABEL.get(kind, kind)}. Choose ONE angle by its key" + (" (the one that fits the team's notes best)" if context else '') +
                f" and write its headline and caption.\nAngles:\n{opts}\n" + (f"Team notes for this post:\n{context}\n" if context else '') +
                (f"Images from the team (look at them with Read if useful): {', '.join(images)}\n" if images else ''))
        schema = COPY_SCHEMA
    if prev: task += f"\nPrevious version: {json.dumps(prev, ensure_ascii=False)}\nChange requested by the team: {note}\nApply the change; keep what they didn't ask to change."
    fallback = angles[0] if angles else None
    for attempt in range(2):
        try:
            out = claude_json(base + '\n\n' + task, schema, images)
        except Exception as e:
            log('copy: claude failed:', e); break
        if kind == 'custom':
            text = ' '.join([out.get('kicker', ''), out.get('headline', ''), out.get('body', '')])
            probs = caption_problems(out.get('caption', ''), ok) + text_problems(text, ok, 'image text')
            if len(out.get('headline', '')) > 60: probs.append('headline too long')
            stats = [s for s in out.get('stats', []) if s.get('id') in data.by_id and s.get('metric') in METRIC and (s['metric'] != 'day' or 'dayRoi' in data.by_id[s['id']])][:3]
            if not probs: return None, out['headline'].strip(), out['caption'].strip(), {'kicker': out['kicker'].strip().upper()[:40], 'body': out['body'].strip(), 'stats': stats}
        else:
            ang = next((a for a in angles if a['key'] == out.get('angle')), fallback)
            probs = caption_problems(out.get('caption', ''), ok) + text_problems(out.get('headline', ''), ok, 'headline')
            if len(out.get('headline', '')) > 60: probs.append('headline too long (max 7 words)')
            if ang['key'].endswith('community') and COMMUNITY not in out.get('caption', ''): probs.append(f'caption must contain {COMMUNITY}')
            if not probs: return ang, out['headline'].strip(), out['caption'].strip(), {}
        log('copy: rejected', probs); task += f"\nYour last answer was rejected: {'; '.join(probs)}. Fix that."
    if kind == 'custom':
        return None, (context.split('\n')[0][:50] or 'From the team.'), 'From the PIP Farming team.', {'kicker': 'PIP FARMING', 'body': '', 'stats': []}
    return fallback, fallback['headline'], fallback['caption'], {}

# ---------- rendering ----------
CSS = """
*{box-sizing:border-box}
body{margin:0;width:1080px;height:1350px;font-family:-apple-system,"Helvetica Neue",Arial;color:#fff;background:#030822;overflow:hidden;position:relative}
.bg{position:absolute;inset:0;background:radial-gradient(55% 32% at 88% 6%,rgba(46,118,255,.55),rgba(46,118,255,0) 70%),radial-gradient(45% 30% at 4% 34%,rgba(24,82,230,.32),rgba(24,82,230,0) 70%),
 radial-gradient(70% 40% at 60% 104%,rgba(20,70,210,.38),rgba(20,70,210,0) 70%),linear-gradient(180deg,#05123a 0%,#030b2a 48%,#010618 100%)}
.bg:after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(118deg,rgba(110,160,255,.045) 0 3px,rgba(0,0,0,0) 3px 120px);filter:blur(2px);-webkit-mask-image:linear-gradient(180deg,#000 0%,rgba(0,0,0,.4) 60%,#000 100%)}
.page{position:relative;height:1350px;padding:64px 72px 48px;display:flex;flex-direction:column}
header{display:flex;justify-content:space-between;align-items:center;flex:none}.logo{height:64px}.date{font-size:22px;color:#a9bdf5}
main{flex:1;min-height:0;display:flex;flex-direction:column;padding-top:46px;overflow:hidden}
footer{flex:none;padding-top:22px}.src{font-size:17px;letter-spacing:1px;color:#8fa6e8;margin-bottom:10px}
.disc{font-size:17px;color:#7f93c9;line-height:1.35}
.k{font-size:24px;letter-spacing:6px;color:#8fb0ff;font-weight:600;white-space:nowrap;overflow:hidden}
h1{margin:16px 0 0;font-size:76px;line-height:1.04;font-weight:800;letter-spacing:-2px;max-height:166px;overflow:hidden}
.sub{margin-top:18px;font-size:30px;color:#c9d6ff;line-height:1.3}
.big{margin-top:34px;font-size:200px;font-weight:900;letter-spacing:-6px;color:#4dff9a;line-height:1.05;white-space:nowrap;text-shadow:0 0 50px rgba(77,255,154,.22)}
.big.w{color:#fff;text-shadow:0 0 60px rgba(61,139,255,.6)}
.glass{background:rgba(10,30,100,.45);border:1px solid rgba(120,160,255,.25);border-radius:26px}
.chip{display:inline-block;font-size:20px;letter-spacing:3px;color:#9fc0ff;border:1px solid rgba(120,160,255,.4);border-radius:40px;padding:8px 18px;vertical-align:middle;text-transform:uppercase}
.who{margin-top:26px;font-size:36px;font-weight:700;display:flex;align-items:center;gap:20px}
.fill{flex:1;min-height:20px}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.c{padding:26px 30px}.c b{display:block;font-size:52px;font-weight:800;letter-spacing:-1px;white-space:nowrap}.c span{display:block;margin-top:6px;font-size:19px;letter-spacing:3px;color:#8fa6e8}
.g{color:#4dff9a}.r{color:#ff8a8a}
.tbl{margin-top:34px;flex:1;min-height:0;display:flex;flex-direction:column;padding:10px 30px}
.hd,.row{display:grid;grid-template-columns:52px 1fr 230px 170px 170px;align-items:center}
.hd{flex:none;font-size:18px;letter-spacing:3px;color:#7f9be6;padding:16px 0;border-bottom:1px solid rgba(120,160,255,.2)}.hd div:nth-child(n+3){text-align:right}
.rows{flex:1;display:flex;flex-direction:column}
.row{flex:1;max-height:130px;font-size:31px;border-bottom:1px solid rgba(120,160,255,.12)}.row:last-child{border-bottom:0}
.row>div:nth-child(n+3){text-align:right;white-space:nowrap}.rk{color:#7f9be6;font-weight:700}
.nm{font-weight:700;white-space:nowrap;overflow:hidden}.nm span{display:block;font-size:17px;letter-spacing:2px;color:#8fa6e8;font-weight:500;margin-top:4px;text-transform:uppercase}
.row .lead{font-weight:800}.wk .hd,.wk .row{grid-template-columns:52px 1fr 250px 230px 140px}.wk .row{font-size:29px}.wk small{font-size:20px;font-weight:600;margin-left:4px}.hd .lead{color:#c9d6ff}
.blur .num{filter:blur(11px);opacity:.9}
.lst{margin-top:30px;padding:8px 30px;flex:none}
.li{display:grid;grid-template-columns:52px 1fr auto;align-items:center;font-size:29px;padding:15px 0;border-bottom:1px solid rgba(120,160,255,.12)}.li:last-child{border-bottom:0}
.li.on{color:#4dff9a;font-weight:800}.li .v{font-weight:700;text-align:right}
.proof{flex:1;display:flex;flex-direction:column;gap:22px;margin-top:40px}.proof .c{flex:1}
.proof .c{display:flex;flex-direction:column;justify-content:center;gap:4px;padding:30px 40px}.proof .c b{font-size:110px;color:#fff;text-shadow:0 0 40px rgba(61,139,255,.6)}.proof .c span{font-size:24px;letter-spacing:4px;text-transform:uppercase}
.styles{flex:1;margin-top:36px;display:flex;flex-direction:column;gap:14px}
.styles .c{flex:1;max-height:140px;padding:0 34px;display:flex;align-items:center;justify-content:space-between}.styles .c b{font-size:40px}.styles .c span{margin:0;font-size:18px}
.inv{flex:1;margin-top:40px;display:flex;flex-direction:column;gap:18px}
.inv .c{flex:1;max-height:150px;display:flex;align-items:center;padding:0 36px;font-size:34px;font-weight:600}.inv .c i{font-style:normal;color:#7fc4ff;margin-right:14px}
.link{margin-top:30px;flex:none;align-self:flex-start;font-size:40px;font-weight:800;background:#fff;color:#06144a;border-radius:60px;padding:22px 40px}
.mark{position:absolute;right:72px;top:250px;width:300px;opacity:.12}
.img{margin-top:30px;flex:1;min-height:0;display:flex;align-items:center;justify-content:center;padding:18px;overflow:hidden}
.img img{max-width:100%;max-height:100%;border-radius:18px;object-fit:contain}
.teaser h1{font-size:120px;max-height:260px;letter-spacing:-4px}
.plogo{display:block;width:auto;height:auto;max-height:64px;max-width:250px}
.who .plogo{max-height:150px;max-width:600px}.li .plogo{max-height:46px;max-width:210px}.nm .plogo{max-width:100%}.styles .c .plogo{max-height:84px;max-width:400px}
.cards.n3 .c{padding:24px 26px}.cards.n3 .c b{font-size:44px}
/* number-first templates (Akhil's look) */
.ttl{font-size:94px;font-weight:800;letter-spacing:-2.5px;line-height:1;white-space:nowrap}
.chip2{align-self:flex-start;display:flex;align-items:center;gap:14px;margin-top:24px;padding:12px 22px;border-radius:12px;background:rgba(255,255,255,.06);border:1px solid rgba(150,175,255,.28);font-size:27px;color:#e3eaff}
.chip2 svg{width:30px;height:30px}
.box{background:linear-gradient(180deg,rgba(52,72,140,.42),rgba(22,34,82,.52));border:1px solid rgba(150,175,255,.24);border-radius:22px}
.ico{flex:none;width:78px;height:78px;border-radius:16px;background:linear-gradient(180deg,#3584ff,#1452d6);display:flex;align-items:center;justify-content:center;font-size:38px;font-weight:800;box-shadow:0 8px 24px rgba(20,82,214,.35)}
.ico svg{width:40px;height:40px}
.t2{margin-top:30px;flex:none;border-radius:20px;overflow:hidden;border:1px solid rgba(150,175,255,.26);background:rgba(16,26,70,.5)}
.th,.tr{display:grid;grid-template-columns:72px 1fr 218px 178px 214px}
.th>div,.tr>div{display:flex;align-items:center;justify-content:center;white-space:nowrap}
.th{background:linear-gradient(180deg,#2c78ff,#1554da);height:70px;font-size:22px;font-weight:700;letter-spacing:1.5px}
.tr{height:104px;font-size:30px;border-top:1px solid rgba(150,175,255,.16)}.tr>div+div,.th>div+div{border-left:1px solid rgba(150,175,255,.16)}
.tr .rk{color:#c9d6ff;font-weight:600}.tr .nm{justify-content:flex-start;padding:0 18px;overflow:hidden}.th .nl{justify-content:flex-start;padding-left:18px}
.pill{display:inline-block;padding:9px 18px;border-radius:12px;font-weight:800;font-size:29px;color:#fff;background:#14b86b}.pill.r{background:#e8312f;color:#fff}.pill.g{color:#fff}
.tr .g:not(.pill){color:#4dff9a}.tr .r:not(.pill){color:#ff7a7a}
.cards3{margin-top:24px;flex:none;display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.sc{display:flex;align-items:center;gap:16px;padding:20px 18px}.sc .ico{width:64px;height:64px}.sc .ico svg{width:32px;height:32px}
.sc span{display:block;font-size:19px;color:#b9c7f2}.sc b{display:block;font-size:36px;font-weight:800;letter-spacing:-.5px;white-space:nowrap;margin-top:2px}.sc small{display:block;font-size:17px;color:#8fa6e8;margin-top:2px;white-space:nowrap;overflow:hidden}
.sc b.g{color:#4dff9a}.sc b.r{color:#ff7a7a}
.follow{margin-top:auto;padding-top:20px;display:flex;align-items:center;justify-content:center;gap:14px;font-size:28px;color:#e3eaff}
.follow i{width:30px;height:30px;border-radius:50%;background:#fff;box-shadow:0 0 0 7px #1c4fd0}.follow b{color:#3d8bff;font-weight:800}
.spot{display:flex;flex-direction:column;align-items:center;margin-top:10px;flex:none}.spot .plogo{max-height:190px;max-width:760px}
.spot .lab{margin-top:22px;font-size:30px;letter-spacing:7px;color:#dfe7ff;font-weight:600}
.g6{margin-top:36px;display:grid;gap:20px;flex:none}
.sb{padding:30px 26px;text-align:center}.sb span{display:block;font-size:20px;letter-spacing:2.5px;color:#b9c7f2;font-weight:600}.sb b{display:block;margin-top:12px;font-size:58px;font-weight:800;letter-spacing:-1px;white-space:nowrap}
.sb b.g{color:#4dff9a}.sb b.r{color:#ff7a7a}
.rk5{margin-top:22px;padding:6px 30px 4px}.rk5 .rkh{font-size:19px;letter-spacing:3px;color:#8fb0ff;font-weight:600;padding:14px 0 6px}
.rk5 .li{padding:9px 0;font-size:28px}.rk5 .li.on{background:rgba(61,139,255,.16);margin:0 -30px;padding:9px 30px}.rk5 .v.g{color:#4dff9a}.rk5 .v.r{color:#ff7a7a}.rk5 .li.on .rk{color:#fff}
"""

FIT_JS = """<script>
(function(){
 function over(e){var f=parseFloat(getComputedStyle(e).fontSize)||16;return e.scrollHeight>e.clientHeight+Math.max(3,f*0.2)||e.scrollWidth>e.clientWidth+2}
 document.querySelectorAll('[data-fit]').forEach(function(e){var s=parseFloat(getComputedStyle(e).fontSize),m=+e.dataset.fit;while(over(e)&&s>m){s-=2;e.style.fontSize=s+'px'}});
 var bad=[];document.querySelectorAll('[data-fit],.nm,.k,.c b,main,.row>div,.li>div').forEach(function(e){if(over(e))bad.push((e.className||e.tagName)+':'+e.textContent.trim().slice(0,30))});
 var ft=document.querySelector('footer').getBoundingClientRect().top;
 document.querySelectorAll('main *').forEach(function(e){var r=e.getBoundingClientRect();if(r.height&&getComputedStyle(e).visibility!='hidden'&&(r.bottom>ft+1||r.right>1080-40||r.left<40))bad.push('out:'+(e.className||e.tagName)+':'+e.textContent.trim().slice(0,30))});
 var m=document.createElement('meta');m.name='fitcheck';m.content=bad.length?bad.slice(0,6).join(' | '):'ok';document.head.appendChild(m);
})();
</script>"""

E = html.escape
def card(v, lab, cls=''): return f'<div class="c glass"><b class="{cls}">{E(v)}</b><span>{E(lab)}</span></div>'
METRIC = {'day': ("TODAY'S RESULT", f_day, 'g'), 'roi': ('TOTAL ROI', f_roi, 'g'), 'wr': ('WIN RATE', f_wr, ''), 'dd': ('MAX DRAWDOWN', f_dd, 'r')}
def tone(m, p): return ('g' if p['dayRoi'] >= 0 else 'r') if m == 'day' else METRIC[m][2]

ICON = {   # small white line icons for the boxes
    'trophy': '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/></svg>',
    'chart': '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><path d="M4 20V14M10 20V9M16 20V12M22 20V4"/></svg>',
    'pct': '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><path d="M19 5 5 19"/><circle cx="7" cy="7" r="2.5"/><circle cx="17" cy="17" r="2.5"/></svg>',
    'cal': '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>'}
def title_html(a): return f'<div class="ttl" data-fit="60">{E(a["title"])}</div><div class="chip2">{ICON["cal"]}{E(a["chip"])}</div>'
def follow_html(a): return f'<div class="follow"><i></i>Follow us for <b>{E(a.get("follow") or FOLLOW)}</b></div>'

def body_html(a, data, headline, extra):
    L = a.get('layout'); hl = f'<h1 data-fit="44">{E(headline)}</h1>'
    if L in ('stat', 'profits'):
        p = data.by_id[a['pid']]; green = tone(a['metric'], p) == 'g' and a['metric'] != 'dd'
        top = f'<div class="k">{E(a["kicker"])}</div><div class="big{"" if green else " w"}" data-fit="90">{E(a["big"])}</div>{hl}' \
              f'<div class="who">{logo(p)}<span class="chip">{E(p["strategyType"])}</span></div><div class="fill"></div>'
        if L == 'profits':
            lis = ''.join(f'<div class="li{" on" if pid == a["pid"] else ""}"><div class="rk">{i+1}</div><div class="nm">{logo(data.by_id[pid])}</div><div class="v">{E(v)}</div></div>' for i, (pid, v) in enumerate(a['rank']))
            return top + f'<div class="k" style="font-size:20px">{E(METRIC[a["metric"]][0])} · RANKING</div><div class="lst glass">{lis}</div>'
        keys = [k for k in ('day', 'roi', 'wr', 'dd') if k != a['metric'] and (k != 'day' or 'dayRoi' in p)]
        cards = [card(METRIC[k][1](p), METRIC[k][0], tone(k, p)) for k in keys]
        return top + f'<div class="cards n{len(cards)}" style="grid-template-columns:repeat({len(cards)},1fr)">{"".join(cards)}</div>'
    if L in ('board', 'teaser'):
        s = a['sort']; order = [s] + [x for x in ('roi', 'day', 'wr', 'dd') if x != s and (x != 'day' or data.has_day)][:2]   # long total ROI never in the narrow last column
        cols = {'day': 'TODAY', 'roi': 'TOTAL ROI', 'wr': 'WIN RATE', 'dd': 'MAX DD'}
        hd = '<div>#</div><div>STRATEGY</div>' + ''.join(f'<div class="{"lead" if i == 0 else ""}">{E(cols[c])}</div>' for i, c in enumerate(order))
        rows = ''.join(f'<div class="row"><div class="rk">{i+1}</div><div class="nm">{logo(data.by_id[pid])}<span>{E(data.by_id[pid]["strategyType"])}</span></div>' +
                       ''.join(f'<div class="num {tone(c, data.by_id[pid])}{" lead" if j == 0 else ""}">{E(METRIC[c][1](data.by_id[pid]))}</div>' for j, c in enumerate(order)) + '</div>'
                       for i, pid in enumerate(a['rows']))
        sub = '<div class="sub">Real strategies. Real results.</div>' if L == 'teaser' else ''
        return f'<div class="k">{E(a["kicker"])}</div>{hl}{sub}<div class="tbl glass{" blur" if a.get("blur") else ""}"><div class="hd">{hd}</div><div class="rows">{rows}</div></div>'
    if L == 'weekly':
        hd = '<div>#</div><div>STRATEGY</div><div class="lead">THIS WEEK</div><div>TOTAL ROI</div><div>WIN RATE</div>'
        rows = ''
        for i, r in enumerate(a['rows']):
            p = data.by_id[r['id']]
            ch = f'{r["d_roi"]:+,.2f}%' if r['d_roi'] is not None else 'New'
            rows += f'<div class="row"><div class="rk">{i+1}</div><div class="nm">{logo(p)}<span>{E(p["strategyType"])}</span></div><div class="lead {"g" if (r["d_roi"] or 0) >= 0 else "r"}">{ch}</div><div>{E(f_roi(p))}</div><div>{E(f_wr(p))}</div></div>'
        return f'<div class="k">{E(a["kicker"])}</div>{hl}<div class="sub">Change in total ROI over the last 7 days.</div><div class="tbl glass wk"><div class="hd">{hd}</div><div class="rows">{rows}</div></div>'
    if L == 'styles':
        cs = ''.join(f'<div class="c glass">{logo(p)}<span>{E(p["strategyType"].upper())}</span></div>' for p in data.ps)
        return f'<div class="k">{E(a["kicker"])}</div>{hl}<div class="styles">{cs}</div>'
    if L == 'community':
        items = ''.join(f'<div class="c glass"><i>—</i>{E(t)}</div>' for t in ('Questions about a strategy', "Today's results, talked through", 'People who copy them, not bots'))
        return f'<img class="mark" src="file://{BRAND}/mark.png"><div class="k">{E(a["kicker"])}</div>{hl}<div class="inv">{items}</div><div class="link">{E(COMMUNITY)}</div>'
    if L == 'table':
        hd = '<div>#</div><div class="nl">STRATEGY</div>' + ''.join(f'<div>{E(c)}</div>' for c in a['cols'])
        rows = ''.join(f'<div class="tr"><div class="rk">{i+1}</div><div class="nm">{logo(data.by_id[r["id"]])}</div>' +
                       ''.join(f'<div><span class="{"pill " if pill else ""}{cls}">{E(t)}</span></div>' for t, cls, pill in r['cells']) + '</div>' for i, r in enumerate(a['rows']))
        cards = ''.join(f'<div class="sc box"><div class="ico">{ICON[ic]}</div><div style="min-width:0"><span>{E(l)}</span><b class="{cls}">{E(v)}</b><small>{E(sub)}</small></div></div>' for ic, l, v, sub, cls in a['cards'])
        return f'{title_html(a)}<div class="t2"><div class="th">{hd}</div>{rows}</div><div class="cards3">{cards}</div>{follow_html(a)}'
    if L == 'spot':
        p = data.by_id[a['pid']]
        cells = ([("TODAY", f_day(p), sign(p['dayRoi']))] if 'dayRoi' in p else []) + ([("THIS WEEK", f_week(p), sign(p['weekRoi']))] if 'weekRoi' in p else []) + \
                [('TOTAL ROI', f_roi(p), 'g'), ('WIN RATE', f_wr(p), ''), ('MAX DRAWDOWN', f_dd(p), 'r'), ('STYLE', p['strategyType'], '')]
        cells = cells[:6] if len(cells) >= 6 else cells[:4]
        cs = ''.join(f'<div class="sb box"><span>{E(l)}</span><b class="{cls}">{E(v)}</b></div>' for l, v, cls in cells)
        m = 'day' if 'dayRoi' in p else 'roi'; ranked = sorted([q for q in data.ps if m != 'day' or 'dayRoi' in q], key=lambda q: -q['dayRoi' if m == 'day' else 'allTimeRoi'])
        lis = ''.join(f'<div class="li{" on" if q["id"] == p["id"] else ""}"><div class="rk">{i+1}</div><div class="nm">{logo(q)}</div><div class="v {tone(m, q)}">{E(METRIC[m][1](q))}</div></div>' for i, q in enumerate(ranked))
        return (f'<div class="spot">{logo(p)}<div class="lab">PERFORMANCE UPDATE</div></div>'
                f'<div class="g6" style="grid-template-columns:repeat({3 if len(cells) == 6 else 2},1fr)">{cs}</div>'
                f'<div class="lst box rk5"><div class="rkh">{"TODAY" if m == "day" else "TOTAL ROI"} · RANKING</div>{lis}</div>{follow_html(a)}')
    if L == 'custom':
        out = f'<div class="k">{E(extra.get("kicker") or "PIP FARMING")}</div>{hl}'
        if extra.get('body'): out += f'<div class="sub" data-fit="22" style="max-height:130px;overflow:hidden">{E(extra["body"])}</div>'
        img = extra.get('image')
        if img: out += f'<div class="img glass"><img src="file://{E(img)}"></div>'
        else: out += '<div class="fill"></div>'
        stats = extra.get('stats') or []
        if stats:
            cs = ''.join(card(METRIC[s['metric']][1](data.by_id[s['id']]), f"{nm(data.by_id[s['id']]).upper()} · {METRIC[s['metric']][0]}", tone(s['metric'], data.by_id[s['id']])) for s in stats)
            out += f'<div class="cards" style="margin-top:24px;grid-template-columns:repeat({len(stats)},1fr)">{cs}</div>'.replace('<span>', '<span style="font-size:15px;letter-spacing:2px">')
        return out
    raise ValueError(f'unknown layout {L}')


# ---------- creative pages (full-bleed art, emoji, glow) ----------
CREATIVE_CSS = """*{box-sizing:border-box}body{margin:0;width:1080px;height:1350px;overflow:hidden;position:relative;font-family:-apple-system,"SF Pro Display","Helvetica Neue",Arial;color:#fff;background:#03081c}
.emo{font-family:"Apple Color Emoji";line-height:1}.top{position:absolute;left:64px;right:64px;top:56px;display:flex;justify-content:space-between;align-items:center;z-index:5}
.top img{height:62px}.tag{font-size:26px;font-weight:800;letter-spacing:3px;padding:12px 22px;border-radius:40px}
.disc{position:absolute;left:64px;right:64px;bottom:40px;font-size:16px;color:rgba(220,230,255,.6);line-height:1.35;z-index:5}.disc b{color:rgba(255,255,255,.8)}
.chips{display:flex;gap:18px}.chip{padding:18px 26px;border-radius:20px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.16)}
.chip span{display:block;font-size:18px;letter-spacing:3px;color:rgba(230,236,255,.7);font-weight:600}.chip b{display:block;font-size:40px;font-weight:800;margin-top:4px;white-space:nowrap}
.art{position:absolute;inset:0;background-position:center;background-size:cover}.free{position:absolute;inset:0;overflow:hidden}
/* fire */
.fire{background:radial-gradient(70% 45% at 50% 100%,rgba(255,92,0,.55),rgba(255,92,0,0) 70%),radial-gradient(60% 40% at 50% 62%,rgba(200,30,0,.35),rgba(0,0,0,0) 70%),linear-gradient(180deg,#07061a 0%,#0b0718 45%,#1a0604 100%)}
.em{position:absolute;border-radius:50%;background:#ffb347;box-shadow:0 0 12px 4px rgba(255,120,0,.8)}
.fire .tag{background:linear-gradient(90deg,#ff3d00,#ff9a00);box-shadow:0 0 30px rgba(255,90,0,.6)}
.ye{position:absolute;top:190px;left:0;right:0;display:flex;justify-content:center;filter:drop-shadow(0 0 30px rgba(80,160,255,.5))}
.flame{position:absolute;left:50%;top:330px;transform:translateX(-50%);font-size:420px;filter:drop-shadow(0 0 60px rgba(255,80,0,.9))}
.side{position:absolute;font-size:110px;filter:drop-shadow(0 0 30px rgba(255,80,0,.8))}
.fbig{position:absolute;left:0;right:0;top:752px;text-align:center;font-size:178px;font-weight:900;letter-spacing:-6px;font-style:italic;line-height:1;
 background:linear-gradient(180deg,#fff7c2 0%,#ffd23f 35%,#ff7a00 70%,#e01e00 100%);-webkit-background-clip:text;color:transparent;filter:drop-shadow(0 0 28px rgba(255,90,0,.75)) drop-shadow(0 6px 0 rgba(120,20,0,.6))}
.flab{position:absolute;left:0;right:0;top:955px;text-align:center;font-size:30px;letter-spacing:10px;font-weight:700;color:#ffd9b0}
.fc{position:absolute;left:64px;right:64px;top:1050px;justify-content:center}.fc .chip{border-color:rgba(255,150,60,.35);background:rgba(255,90,0,.10)}
/* grid */
.gshade{position:absolute;inset:0;background:linear-gradient(180deg,rgba(2,8,20,.85) 0%,rgba(2,8,20,.35) 40%,rgba(2,8,20,.55) 70%,rgba(2,8,20,.92) 100%)}
.gplain{background:radial-gradient(60% 45% at 85% 60%,rgba(0,140,255,.35),rgba(0,0,0,0) 70%),radial-gradient(50% 35% at 10% 10%,rgba(30,90,255,.3),rgba(0,0,0,0) 70%),linear-gradient(180deg,#041030,#020818)}
.grid .tag{background:linear-gradient(90deg,#00c6ff,#0072ff);box-shadow:0 0 30px rgba(0,160,255,.6)}
.cw{position:absolute;top:170px;left:64px;filter:drop-shadow(0 0 24px rgba(0,180,255,.55))}
.gh{position:absolute;left:64px;top:340px;font-size:118px;font-weight:900;letter-spacing:-4px;line-height:.95}.gh em{font-style:normal;background:linear-gradient(180deg,#a8f5ff,#00d0ff 60%,#008cff);-webkit-background-clip:text;color:transparent}
.gs{position:absolute;left:64px;top:470px;font-size:38px;font-weight:600;color:#d7ecff}
.dots{position:absolute;left:64px;top:560px;width:560px;padding:26px;display:grid;grid-template-columns:repeat(10,1fr);gap:12px;border-radius:26px;background:rgba(2,14,36,.55);border:1px solid rgba(120,200,255,.28)}
.dots i{display:block;aspect-ratio:1;border-radius:50%}.dots .w{background:radial-gradient(circle at 35% 30%,#b9fbff,#18e0a0 55%,#0aa870);box-shadow:0 0 10px rgba(40,255,170,.55)}
.dots .l{border:3px solid #ff5a5a;box-shadow:0 0 8px rgba(255,80,80,.5)}
.leg{position:absolute;left:64px;top:1150px;display:flex;gap:30px;font-size:24px;color:#d7ecff}.leg i{display:inline-block;width:20px;height:20px;border-radius:50%;margin-right:10px;vertical-align:-3px}
.wc{position:absolute;left:670px;right:64px;top:600px;flex-direction:column}.wc .chip{background:rgba(2,14,36,.6);border-color:rgba(120,200,255,.3)}.wc .chip b{color:#3dffb4}
.spirit{position:absolute;right:90px;top:1000px;font-size:150px;filter:drop-shadow(0 0 30px rgba(0,180,255,.6));transform:rotate(-8deg)}
/* podium */
.podium{background:radial-gradient(60% 40% at 50% 78%,rgba(255,190,40,.22),rgba(0,0,0,0) 70%),radial-gradient(60% 40% at 50% 0%,rgba(40,90,255,.35),rgba(0,0,0,0) 70%),linear-gradient(180deg,#050b24,#02050f)}
.podium .tag{background:linear-gradient(90deg,#f5c542,#ff9f1a);color:#1d1200;box-shadow:0 0 30px rgba(255,190,40,.55)}
.q{position:absolute;left:0;right:0;top:170px;text-align:center;font-size:104px;font-weight:900;letter-spacing:-3px}.q2{position:absolute;left:0;right:0;top:300px;text-align:center;font-size:30px;letter-spacing:8px;color:#c9d6ff;font-weight:700}
.pod{position:absolute;left:64px;right:64px;bottom:150px;display:grid;grid-template-columns:1fr 1.12fr 1fr;align-items:end;gap:18px}
.col{display:flex;flex-direction:column;align-items:center}.med{font-size:96px;filter:drop-shadow(0 6px 18px rgba(0,0,0,.6))}
.lgw{height:96px;display:flex;align-items:center;justify-content:center;margin:10px 0 16px}
.blk{width:100%;border-radius:20px 20px 6px 6px;display:flex;flex-direction:column;align-items:center;padding-top:26px;border:1px solid rgba(255,255,255,.25)}
.blk b{font-size:46px;font-weight:900;white-space:nowrap;letter-spacing:-1px}.blk span{font-size:18px;letter-spacing:3px;margin-top:6px;font-weight:700;opacity:.85}.blk .n{font-size:120px;font-weight:900;opacity:.18;margin-top:auto;line-height:1}
.g1{height:500px;background:linear-gradient(180deg,#ffe27a,#f5b70f 40%,#9a6a00);color:#2a1900;box-shadow:0 0 60px rgba(255,200,40,.45)}
.g2{height:390px;background:linear-gradient(180deg,#f4f7ff,#b9c4d9 45%,#5d6a82);color:#141a26}
.g3{height:320px;background:linear-gradient(180deg,#ffc79a,#d9803f 45%,#7a3d12);color:#2a1305}
/* race */
.race{background:radial-gradient(60% 35% at 50% 0%,rgba(40,100,255,.35),rgba(0,0,0,0) 70%),linear-gradient(180deg,#0a1024,#05070f)}
.race .tag{background:linear-gradient(90deg,#ff2e63,#ff9a3c);box-shadow:0 0 30px rgba(255,60,90,.5)}
.ct{position:absolute;left:64px;top:170px;font-size:100px;font-weight:900;letter-spacing:-3px;line-height:1}.cs{position:absolute;left:64px;top:290px;font-size:28px;letter-spacing:8px;color:#c9d6ff;font-weight:700}
.lanes{position:absolute;left:64px;right:64px;top:370px;display:flex;flex-direction:column;gap:18px}
.lane{position:relative;height:150px;border-radius:18px;background:linear-gradient(180deg,#1d2438,#121827);border:1px solid rgba(255,255,255,.09);overflow:hidden}
.lane:before{content:"";position:absolute;left:250px;right:70px;top:50%;border-top:4px dashed rgba(255,255,255,.16)}
.lane:after{content:"";position:absolute;right:0;top:0;bottom:0;width:56px;background:repeating-conic-gradient(#fff 0 25%,#111 0 50%) 0 0/28px 28px;opacity:.85}
.ln{position:absolute;left:22px;top:50%;transform:translateY(-50%)}.car{position:absolute;top:50%;font-size:86px;transform:translate(-50%,-50%) scaleX(-1)}
.lv{position:absolute;top:50%;transform:translateY(-50%);font-size:34px;font-weight:900;white-space:nowrap}.lv.g{color:#4dff9a}.lv.r{color:#ff7a7a}
/* vs */
.vs{background:linear-gradient(105deg,#0b2a8a 0 49.8%,#ffffff 49.8% 50.2%,#4a0d6e 50.2% 100%)}.vs:before{content:"";position:absolute;inset:0;background:radial-gradient(60% 50% at 50% 30%,rgba(0,0,0,0),rgba(0,0,0,.5)),linear-gradient(180deg,rgba(3,5,15,0) 55%,rgba(3,5,15,.97) 80%)}
.vs .tag{background:linear-gradient(90deg,#ffd000,#ff7a00);color:#201000;box-shadow:0 0 30px rgba(255,180,0,.5)}
.side2{position:absolute;top:190px;width:430px;display:flex;flex-direction:column;align-items:center;gap:14px}.side2 small{font-size:20px;letter-spacing:4px;color:rgba(255,255,255,.75);font-weight:700}
.vsb{position:absolute;left:50%;top:205px;transform:translateX(-50%);width:150px;height:150px;border-radius:50%;background:#0a0a14;border:4px solid #ffd000;display:flex;align-items:center;justify-content:center;font-size:62px;font-weight:900;font-style:italic;color:#ffd000;box-shadow:0 0 40px rgba(255,200,0,.6)}
.vrows{position:absolute;left:64px;right:64px;top:450px;display:flex;flex-direction:column;gap:16px}
.vr{display:grid;grid-template-columns:1fr 260px 1fr;align-items:center;height:136px;border-radius:20px;background:rgba(5,8,25,.55);border:1px solid rgba(255,255,255,.14)}
.vr b{text-align:center;font-size:48px;font-weight:900;white-space:nowrap}.vr span{text-align:center;font-size:20px;letter-spacing:3px;color:#d8defa;font-weight:700}
.vr b.win{color:#4dff9a;text-shadow:0 0 22px rgba(77,255,154,.45)}.vr b.win:after{content:" 👑";font-family:"Apple Color Emoji";font-size:34px}
.vq{margin-top:44px;text-align:center;font-size:56px;font-weight:900}
/* gauge */
.gauge{background:radial-gradient(60% 40% at 50% 45%,rgba(40,200,140,.22),rgba(0,0,0,0) 70%),linear-gradient(180deg,#041a22,#020a10)}
.gauge .tag{background:linear-gradient(90deg,#16c784,#0fa3b1);box-shadow:0 0 30px rgba(22,199,132,.5)}
.glogo{position:absolute;left:0;right:0;top:330px;display:flex;justify-content:center}.gsvg{position:absolute;left:160px;top:440px}
.gv{position:absolute;left:0;right:0;top:830px;text-align:center;font-size:130px;font-weight:900;letter-spacing:-4px;line-height:1}.gl{position:absolute;left:0;right:0;top:975px;text-align:center;font-size:26px;letter-spacing:8px;color:#bfeee0;font-weight:700}
.gch{position:absolute;left:64px;right:64px;top:1050px;justify-content:center}.shield{position:absolute;right:70px;top:150px;font-size:150px;transform:rotate(10deg);filter:drop-shadow(0 0 30px rgba(22,199,132,.6))}
/* cards */
.cards{background:radial-gradient(60% 40% at 50% 10%,rgba(120,60,255,.35),rgba(0,0,0,0) 70%),linear-gradient(180deg,#0b0a24,#04040f)}
.cards .tag{background:linear-gradient(90deg,#a855f7,#6366f1);box-shadow:0 0 30px rgba(140,90,255,.55)}
.deck{position:absolute;left:64px;right:64px;top:330px;display:flex;flex-wrap:wrap;justify-content:center;gap:24px}
.card{width:290px;height:420px;border-radius:24px;padding:3px;background:linear-gradient(160deg,#5b8cff,#1d3fb0)}.card.c1{background:linear-gradient(160deg,#fff1a6,#e0a800 50%,#8a5a00);box-shadow:0 0 50px rgba(255,200,40,.45)}
.card.c2{background:linear-gradient(160deg,#ffffff,#9aa6bd 50%,#56607a)}.card.c3{background:linear-gradient(160deg,#ffd2ad,#c46a2b 50%,#6a3210)}
.ci{height:100%;border-radius:21px;background:linear-gradient(180deg,#111a3a,#070b1c);display:flex;flex-direction:column;align-items:center;padding:22px 18px;position:relative}
.ci .rk{position:absolute;left:18px;top:14px;font-size:44px;font-weight:900;opacity:.9}.ci .cb{margin-top:46px;font-size:46px;font-weight:900;white-space:nowrap}.ci .cl{font-size:15px;letter-spacing:3px;color:#9fb2e8;font-weight:700}
.ci .clg{height:110px;display:flex;align-items:center;justify-content:center;margin:12px 0}.ci .cst{width:100%;display:flex;justify-content:space-between;border-top:1px solid rgba(255,255,255,.12);padding-top:12px;font-size:15px;color:#9fb2e8;letter-spacing:1px}
.ci .cst b{display:block;font-size:24px;color:#fff;letter-spacing:0;margin-top:2px}
/* club */
.club{background:radial-gradient(55% 40% at 50% 42%,rgba(255,190,40,.28),rgba(0,0,0,0) 70%),linear-gradient(180deg,#1a0b3a,#07041a)}
.club .tag{background:linear-gradient(90deg,#f5c542,#ff6ec7);color:#1d0a20;box-shadow:0 0 30px rgba(255,150,200,.5)}
.cf{position:absolute;border-radius:2px}.badge{position:absolute;left:50%;top:300px;transform:translateX(-50%);width:540px;height:540px;border-radius:50%;padding:18px;background:conic-gradient(#fff4b0,#e0a800,#fff4b0,#b07800,#fff4b0);box-shadow:0 0 80px rgba(255,200,40,.55)}
.bin{width:100%;height:100%;border-radius:50%;background:radial-gradient(circle at 50% 35%,#2a1b5c,#0d0826);display:flex;flex-direction:column;align-items:center;justify-content:center}
.bin b{font-size:150px;font-weight:900;letter-spacing:-5px;line-height:1;background:linear-gradient(180deg,#fff7c2,#ffd23f 50%,#e09a00);-webkit-background-clip:text;color:transparent}.bin span{font-size:40px;letter-spacing:16px;font-weight:800;color:#ffe9a6;margin-top:8px;padding-left:16px}
.blogo{position:absolute;left:0;right:0;top:880px;display:flex;justify-content:center}.bt{position:absolute;left:0;right:0;top:1010px;text-align:center;font-size:40px;font-weight:800}.bt em{font-style:normal;color:#4dff9a}
/* ticker */
.ticker{background:linear-gradient(180deg,#0b0f1a,#030508)}.ticker .tag{background:linear-gradient(90deg,#39ff88,#00b86b);color:#00210f;box-shadow:0 0 30px rgba(57,255,136,.45)}
.led{position:absolute;left:64px;right:64px;top:330px;border-radius:24px;background:#020304;border:2px solid #1f2633;box-shadow:inset 0 0 60px rgba(0,0,0,.9),0 0 50px rgba(57,255,136,.12);padding:24px 30px;
 background-image:radial-gradient(rgba(255,255,255,.05) 1px,rgba(0,0,0,0) 1.6px);background-size:7px 7px;font-family:Menlo,"SF Mono",monospace}
.lh{display:flex;justify-content:space-between;color:#ffb000;font-size:22px;letter-spacing:4px;text-shadow:0 0 12px rgba(255,176,0,.7);padding-bottom:14px;border-bottom:2px dashed #2a3140}
.lr{display:grid;grid-template-columns:1fr 250px 190px;align-items:center;height:138px;border-bottom:2px dashed #1c2230}.lr:last-child{border-bottom:0}
.lr .up{color:#39ff88;font-size:42px;font-weight:700;text-align:right;text-shadow:0 0 14px rgba(57,255,136,.8);white-space:nowrap}.lr .wr{color:#ffb000;font-size:34px;text-align:right;text-shadow:0 0 12px rgba(255,176,0,.7)}
/* guess */
.guess{background:radial-gradient(60% 40% at 50% 40%,rgba(0,200,255,.22),rgba(0,0,0,0) 70%),linear-gradient(180deg,#05162a,#020814)}
.guess .tag{background:linear-gradient(90deg,#00e0ff,#7c4dff);box-shadow:0 0 30px rgba(0,200,255,.5)}
.myst{position:absolute;left:110px;right:110px;top:330px;height:620px;border-radius:30px;background:rgba(8,20,45,.6);border:2px dashed rgba(120,200,255,.45);display:flex;flex-direction:column;align-items:center;padding-top:40px}
.mlogo{position:relative;height:200px;display:flex;align-items:center;justify-content:center}.mlogo img{filter:blur(22px) brightness(.8);opacity:.85}
.qm{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-size:210px;font-weight:900;color:#fff;text-shadow:0 0 40px rgba(0,200,255,.8)}
.mst{margin-top:36px;display:grid;grid-template-columns:1fr 1fr;gap:18px;width:100%;padding:0 40px}.mst div{padding:18px 22px;border-radius:18px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14)}
.mst span{display:block;font-size:17px;letter-spacing:3px;color:#a9c4ef;font-weight:700}.mst b{display:block;font-size:44px;font-weight:900;margin-top:4px;white-space:nowrap}
.gq{position:absolute;left:0;right:0;top:1030px;text-align:center;font-size:46px;font-weight:900}"""
CREATIVE_FIT_JS = """<script>(function(){var bad=[];document.querySelectorAll('body *').forEach(function(e){if(e.closest('.free,.art'))return;var r=e.getBoundingClientRect();
 if(r.width&&r.height&&(r.right>1081||r.left<-1||r.bottom>1351||r.top<-1))bad.push('out:'+(e.className||e.tagName))});
 var d=document.querySelector('.disc').getBoundingClientRect();document.querySelectorAll('.chips,.leg,.pod,.dots').forEach(function(e){if(e.getBoundingClientRect().bottom>d.top-6)bad.push('over disclaimer:'+e.className)});
 var m=document.createElement('meta');m.name='fitcheck';m.content=bad.length?bad.slice(0,5).join(' | '):'ok';document.head.appendChild(m)})();</script>"""

def creative_page(a, data):
    L = a['layout']; emo = lambda e, cls='', st='': f'<span class="emo {cls}" style="{st}">{e}</span>'
    def lg(p, h, w): return f'<img src="file://{BRAND}/providers/{p["id"]}.png" style="max-height:{h}px;max-width:{w}px;display:block">'
    top = lambda tag: f'<div class="top"><img src="file://{BRAND}/logo-full.png"><div class="tag">{tag}</div></div>'
    if L == 'fire':
        p = data.by_id[a['pid']]; rnd = random.Random(p['id'])
        embers = ''.join(f'<i class="em" style="left:{rnd.randint(0, 1070)}px;top:{rnd.randint(560, 1250)}px;width:{(z := rnd.choice([4, 5, 6, 8, 10]))}px;height:{z}px;opacity:{rnd.uniform(.35, .95):.2f}"></i>' for _ in range(70))
        big, lab = (f_day(p), "TODAY'S RESULT") if a['metric'] == 'day' else (f_roi(p), 'TOTAL ROI · ALL TIME')
        chips = ([('TOTAL ROI', f_roi(p))] if a['metric'] == 'day' else []) + [('WIN RATE', f_wr(p)), ('STYLE', p['strategyType'])]
        body = (f'<div class="free">{embers}</div>{top(emo("🔥") + " ON FIRE")}{emo("🔥", "flame")}<div class="ye">{lg(p, 170, 760)}</div>'
                f'{emo("🔥", "side", "left:70px;top:600px;transform:rotate(-14deg)")}{emo("🔥", "side", "right:70px;top:560px;transform:rotate(12deg);font-size:130px")}'
                f'<div class="fbig">{E(big)}</div><div class="flab">{lab}</div>'
                f'<div class="chips fc">' + ''.join(f'<div class="chip"><span>{l}</span><b>{E(v)}</b></div>' for l, v in chips[:3]) + '</div>')
    elif L == 'grid':
        p = data.by_id[a['pid']]; n = round(p['winRate']); art = os.path.join(BRAND, 'art', f'{p["id"]}.jpg')
        bg = (f'<div class="art" style="background-image:url(file://{art});filter:brightness(1.55) saturate(1.35)"></div><div class="gshade"></div>' if os.path.exists(art)
              else '<div class="art gplain"></div>')
        dots = ''.join(f'<i class="{"w" if k < n else "l"}"></i>' for k in range(100))
        body = (f'{bg}{top(emo("🎯") + " WIN RATE")}<div class="cw">{lg(p, 120, 520)}</div><div class="gh"><em>{n}</em> out of 100</div><div class="gs">trades closed in profit</div>'
                f'<div class="dots">{dots}</div><div class="leg"><span><i style="background:#18e0a0"></i>Win</span><span><i style="border:3px solid #ff5a5a"></i>Loss</span></div>'
                f'<div class="chips wc"><div class="chip"><span>WIN RATE</span><b>{E(f_wr(p))}</b></div><div class="chip"><span>TOTAL ROI</span><b>{E(f_roi(p))}</b></div></div>'
                f'{emo(SPIRIT.get(p["id"], "📊"), "spirit")}')
    elif L == 'race':
        ps = [data.by_id[i] for i in a['rows']]; m = a['metric']; v = (lambda p: p['dayRoi']) if m == 'day' else (lambda p: p['allTimeRoi'])
        lo, hi = min(map(v, ps)), max(map(v, ps))
        lanes = ''.join(f'<div class="lane"><div class="ln">{lg(p, 56, 200)}</div>{emo("🏎️", "car", f"left:{x}px")}<div class="lv {tone(m, p)}" style="left:{x + 56}px">{E(METRIC[m][1](p))}</div></div>'
                        for p in ps for x in [round(300 + 340 * ((v(p) - lo) / (hi - lo) if hi > lo else 1))])
        body = (f'{top(emo("🏁") + " THE RACE")}<div class="ct">{"Today&#39;s race" if m == "day" else "The race to the top"}</div>'
                f'<div class="cs">{"TODAY&#39;S RESULT" if m == "day" else "TOTAL ROI · ALL TIME"}</div><div class="lanes">{lanes}</div>')
    elif L == 'vs':
        x, y = (data.by_id[i] for i in a['rows'])
        rows = ([('TODAY', 'dayRoi', f_day, 1)] if a['metric'] == 'day' and 'dayRoi' in x and 'dayRoi' in y else []) + [('TOTAL ROI', 'allTimeRoi', f_roi, 1), ('WIN RATE', 'winRate', f_wr, 1), ('MAX DRAWDOWN', 'drawdown', f_dd, -1)]
        vr = ''.join(f'<div class="vr"><b class="{"win" if (x[k] - y[k]) * d > 0 else ""}">{E(fn(x))}</b><span>{lab}</span><b class="{"win" if (y[k] - x[k]) * d > 0 else ""}">{E(fn(y))}</b></div>' for lab, k, fn, d in rows)
        body = (f'{top(emo("🥊") + " HEAD TO HEAD")}<div class="side2" style="left:64px">{lg(x, 110, 400)}<small>{E(x["strategyType"].upper())}</small></div>'
                f'<div class="side2" style="right:64px">{lg(y, 110, 400)}<small>{E(y["strategyType"].upper())}</small></div><div class="vsb">VS</div>'
                f'<div class="vrows">{vr}<div class="vq">Who&#39;s your pick? {emo("👇")}</div></div>')
    elif L == 'gauge':
        p = data.by_id[a['pid']]; t = math.pi * min(abs(p['drawdown']), 50) / 50; nx, ny = 380 - 290 * math.cos(t), 380 - 290 * math.sin(t)
        svg = (f'<svg class="gsvg" width="760" height="450" viewBox="0 0 760 450"><defs><linearGradient id="gg" x1="0" x2="1"><stop offset="0" stop-color="#16c784"/><stop offset=".5" stop-color="#ffd000"/><stop offset="1" stop-color="#ff3b3b"/></linearGradient></defs>'
               f'<path d="M 60 380 A 320 320 0 0 1 700 380" fill="none" stroke="rgba(255,255,255,.08)" stroke-width="58"/><path d="M 60 380 A 320 320 0 0 1 700 380" fill="none" stroke="url(#gg)" stroke-width="44" stroke-linecap="round"/>'
               f'<text x="60" y="440" fill="#9fd" font-size="24" font-family="-apple-system" text-anchor="middle">0%</text><text x="700" y="440" fill="#f99" font-size="24" font-family="-apple-system" text-anchor="middle">50%</text>'
               f'<line x1="380" y1="380" x2="{nx:.0f}" y2="{ny:.0f}" stroke="#fff" stroke-width="12" stroke-linecap="round"/><circle cx="380" cy="380" r="26" fill="#fff"/></svg>')
        body = (f'{top(emo("🛡️") + " THE CALM ONE")}<div class="ct" style="top:160px">The calm one</div>{emo("🛡️", "shield")}<div class="glogo">{lg(p, 90, 520)}</div>{svg}'
                f'<div class="gv">{E(f_dd(p))}</div><div class="gl">DEEPEST DIP · ALL TIME</div>'
                f'<div class="chips gch"><div class="chip"><span>WIN RATE</span><b>{E(f_wr(p))}</b></div><div class="chip"><span>TOTAL ROI</span><b style="color:#4dff9a">{E(f_roi(p))}</b></div></div>')
    elif L == 'cards':
        ps = [data.by_id[i] for i in a['rows']]; m = a['metric']
        deck = ''.join(f'<div class="card c{i}"><div class="ci"><div class="rk">{i}</div><div class="cb" style="color:{"#4dff9a" if tone(m, p) == "g" else "#ff7a7a"}">{E(METRIC[m][1](p))}</div>'
                       f'<div class="cl">{"TODAY" if m == "day" else "TOTAL ROI"}</div><div class="clg">{lg(p, 90, 240)}</div>'
                       f'<div class="cst"><div>WIN RATE<b>{E(f_wr(p))}</b></div><div style="text-align:right">MAX DD<b>{E(f_dd(p))}</b></div></div></div></div>' for i, p in enumerate(ps, 1))
        body = f'{top(emo("🃏") + " PICK YOUR CARD")}<div class="ct">Pick your card</div><div class="cs">{"TODAY&#39;S RESULT" if m == "day" else "TOTAL ROI · ALL TIME"}</div><div class="deck">{deck}</div>'
    elif L == 'club':
        p = data.by_id[a['pid']]; rnd = random.Random(a['club'] + p['id'])
        conf = ''.join(f'<i class="cf" style="left:{rnd.randint(0, 1060)}px;top:{rnd.randint(130, 1240)}px;width:{rnd.choice([10, 14, 18])}px;height:{rnd.choice([6, 8])}px;'
                       f'background:{rnd.choice(["#ffd23f", "#ff6ec7", "#4dff9a", "#5b8cff", "#ffffff"])};transform:rotate({rnd.randint(0, 180)}deg);opacity:.8"></i>' for _ in range(90))
        body = (f'<div class="free">{conf}</div>{top(emo("🎉") + " MILESTONE")}<div class="ct" style="left:0;right:0;text-align:center;font-size:88px">Milestone unlocked {emo("🎉")}</div>'
                f'<div class="badge"><div class="bin"><b>+{a["club"]}%</b><span>CLUB</span></div></div><div class="blogo">{lg(p, 100, 560)}</div>'
                f'<div class="bt"><em>{E(f_roi(p))}</em> total ROI, all time</div>')
    elif L == 'ticker':
        ps = [data.by_id[i] for i in a['rows']]
        rows = ''.join(f'<div class="lr"><div>{lg(p, 62, 300)}</div><div class="up">▲ {E(f_roi(p))}</div><div class="wr">{E(f_wr(p))}</div></div>' for p in ps)
        body = (f'{top(emo("📟") + " THE BOARD")}<div class="ct">The board</div><div class="cs">ALL-TIME NUMBERS</div>'
                f'<div class="led"><div class="lh"><span>STRATEGY</span><span>TOTAL ROI &nbsp;&nbsp;&nbsp;&nbsp; WIN RATE</span></div>{rows}</div>')
    elif L == 'guess':
        p = data.by_id[a['pid']]
        st = ''.join(f'<div><span>{l}</span><b style="color:{c}">{E(v)}</b></div>' for l, v, c in (('TOTAL ROI', f_roi(p), '#4dff9a'), ('WIN RATE', f_wr(p), '#fff'), ('MAX DRAWDOWN', f_dd(p), '#ff7a7a'), ('STYLE', p['strategyType'], '#fff')))
        body = (f'{top(emo("🤔") + " GUESS WHO")}<div class="ct" style="left:0;right:0;text-align:center;font-size:110px">Guess who? {emo("🤔")}</div>'
                f'<div class="myst"><div class="mlogo">{lg(p, 150, 520)}<div class="qm">?</div></div><div class="mst">{st}</div></div><div class="gq">Drop your guess in the comments {emo("👇")}</div>')
    elif L == 'podium':
        ps = [data.by_id[i] for i in a['rows']]; m = a['metric']
        def col(p, cls, med, rank):
            return (f'<div class="col"><div class="med emo">{med}</div><div class="lgw">{lg(p, 86, 300)}</div><div class="blk {cls}"><b>{E(METRIC[m][1](p))}</b>'
                    f'<span>{"TODAY" if m == "day" else "TOTAL ROI"}</span><div class="n">{rank}</div></div></div>')
        body = (f'{top(emo("🏆") + (" TODAY&#39;S PODIUM" if m == "day" else " THE PODIUM"))}<div class="q">Who&#39;s your pick? {emo("👇")}</div>'
                f'<div class="q2">{"TOP 3 TODAY" if m == "day" else "TOP 3 BY TOTAL ROI"}</div>'
                f'<div class="pod">{col(ps[1], "g2", "🥈", 2)}{col(ps[0], "g1", "🥇", 1)}{col(ps[2], "g3", "🥉", 3)}</div>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CREATIVE_CSS}</style></head><body class="{L}">{body}'
            f'<div class="disc"><b>Disclaimer:</b> {DISCLAIMER}</div>{CREATIVE_FIT_JS}</body></html>')

def page_html(a, data, headline, extra):
    if a.get('layout') in CREATIVE: return creative_page(a, data)
    cls = 'teaser' if a.get('layout') == 'teaser' else ''   # no date, time or place on any post (team, 2026-10-22)
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body class="{cls}"><div class="bg"></div><div class="page">'
            f'<header><img class="logo" src="file://{BRAND}/logo-full.png"></header>'
            f'<main>{body_html(a, data, headline, extra)}</main>'
            f'<footer><div class="disc"><b>Disclaimer:</b> {DISCLAIMER}</div></footer>'
            f'</div>{FIT_JS}</body></html>')

class RenderError(Exception): pass

def chrome(args, timeout=90):
    prof = tempfile.mkdtemp(prefix='pipchrome-')
    try:
        return subprocess.run([CHROME, '--headless', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files', f'--user-data-dir={prof}',
                               '--no-first-run', '--window-size=1080,1350', '--virtual-time-budget=3000'] + args, capture_output=True, text=True, timeout=timeout)
    finally: shutil.rmtree(prof, ignore_errors=True)

def render(doc, png):
    """HTML -> PNG at 2160x2700. Fails (RenderError) if anything overflows or the PNG isn't made."""
    src = png[:-4] + '.html'; open(src, 'w').write(doc)
    dom = chrome(['--dump-dom', f'file://{src}']).stdout
    m = re.search(r'name="fitcheck" content="([^"]*)"', dom)
    if not m: raise RenderError('layout check did not run')
    if m.group(1) != 'ok': raise RenderError('layout: ' + html.unescape(m.group(1)))
    if os.path.exists(png): os.remove(png)
    chrome(['--force-device-scale-factor=2', f'--screenshot={png}', f'file://{src}'])
    if not os.path.exists(png) or os.path.getsize(png) < 50000: raise RenderError('no image')
    with open(png, 'rb') as fh: w, h = int.from_bytes(fh.read(24)[16:20], 'big'), None
    if w != 2160: raise RenderError(f'image is {w}px wide')
    return png

# ---------- drafts ----------
def draft_path(did): return os.path.join(ST, 'drafts', f'{did}.json')
def load_draft(did): return jread(draft_path(did))
def save_draft(d):
    with locked(): jwrite(draft_path(d['id']), d)

def make_draft(kind, data=None, context='', images=(), day=None, auto=False, by=None, prev=None, note=''):
    """Builds (or rebuilds, with prev + note) a post: angle -> copy -> image. Returns the draft dict."""
    data = data or (yesterday_data() if kind == 'results' else load_data())
    ok_extra = []
    if kind == 'custom': angles = []
    elif prev and prev.get('angle_key') and not context and kind != 'weekly':
        angles = angles_for(kind, data, day); keep = [a for a in angles if a['key'] == prev['angle_key']]
        angles = keep + [a for a in angles if a['key'] != prev['angle_key']]
    elif auto: angles = [auto_angle(kind, data, day)]
    else: angles = angles_for(kind, data, day)
    for a in angles: ok_extra += a.get('extra', [])
    ok = data_numbers(data, ok_extra)
    pv = {k: prev.get(k) for k in ('angle_key', 'headline', 'caption', 'extra')} if prev else None
    ang, head, cap, extra = write_copy(kind, angles, data, context, list(images), pv, note, ok)
    if kind == 'custom':
        ang = {'key': 'custom', 'layout': 'custom'}
        if images: extra['image'] = images[0]
    did = (prev or {}).get('id') or 'd' + uuid.uuid4().hex[:7]
    ver = (prev or {}).get('version', 0) + 1
    png = os.path.join(POSTS, 'drafts', f'{did}-v{ver}.png')
    render(page_html(ang, data, head, extra), png)
    d = dict(prev or {}, id=did, type=kind, day=day, angle_key=ang['key'], headline=head, caption=cap, extra=extra, png=png, version=ver,
             as_of=data.as_of.isoformat(), context=context, images=list(images), created=now().isoformat(), by=by, status=(prev or {}).get('status', 'draft'))
    save_draft(d)
    return d

def commit_rotation(d):
    kind = d['type']
    if kind in ('highlight', 'profits', 'results', 'hype'): remember(kind, d['angle_key'])
    remember('captions', d['caption'])

def draft_age_ok(d):
    age = (now() - datetime.datetime.fromisoformat(d['as_of'])).total_seconds() / 3600
    return age <= MAX_AGE_H, age

def publish_draft(d):
    """Sends the draft to the channel. Checks data age, the image and the caption numbers first."""
    ok, age = draft_age_ok(d)
    if not ok: raise Stale(f'data is {age:.0f} h old')
    if not os.path.exists(d['png']): raise RenderError('image missing')
    mid = api('sendPhoto', files={'photo': ('pipfarming.png', d['png'])}, chat_id=CHANNEL, caption=d['caption'], http_timeout=120)['message_id']
    with locked():
        cur = jread(draft_path(d['id']), d); cur.update(status='posted', posted_msg=mid, posted_at=now().isoformat()); jwrite(draft_path(d['id']), cur)
    commit_rotation(d)
    log('posted', d['id'], d['type'], d['angle_key'], 'msg', mid, '|', d['caption'], f='post.log')
    return mid

# ---------- the day plan ----------
def default_slot(date, t):
    if t == '11:00' and date.weekday() == 6: return {'type': 'weekly'}   # Sunday 11 am: the week
    return {'type': DEFAULT_TYPE[t]}

def plan_path(date): return os.path.join(ST, 'plan', f'{date}.json')
def load_plan(date):
    saved = jread(plan_path(date), {})
    plan = {}
    for t in SLOTS:
        # Johannes 2026-10-22: 3 posts a day (13, 17, 20); 9 am only when someone schedules a post there
        s = dict(default_slot(date, t), status='off' if t == '09:00' or (t == '11:00' and date.weekday() == 0) else 'planned', draft=None, previews=[])
        s.update(saved.get(t, {})); plan[t] = s
    return plan

def update_slot(date, t, **kw):
    with locked():
        saved = jread(plan_path(date), {}); s = saved.get(t, {}); s.update(kw); saved[t] = s; jwrite(plan_path(date), saved)
    return load_plan(date)[t]

def slot_dt(date, t): return datetime.datetime.combine(date, datetime.time.fromisoformat(t), TZ)

def slot_label(s):
    return f"Launch day {s['day']}" if s['type'] == 'launch' else LABEL.get(s['type'], s['type'])

def next_free_slot(after=None):
    after = after or now()
    for add in range(0, 8):
        date = after.date() + datetime.timedelta(days=add); plan = load_plan(date)
        for t in SLOTS:
            if slot_dt(date, t) > after + datetime.timedelta(minutes=5) and not plan[t].get('manual') and plan[t]['status'] in ('planned', 'ready'):
                return date, t
    return None

def next_time(t, after=None):
    after = after or now(); date = after.date()
    if slot_dt(date, t) <= after + datetime.timedelta(minutes=5): date += datetime.timedelta(days=1)
    return date, t

# ---------- team guide ----------
def add_rule(text):
    text = ' '.join(text.split())
    with locked():
        f = os.path.join(ST, 'guide.md')
        if not os.path.exists(f): open(f, 'w').write('# PIP Farming post rules (taught by the team; every new post reads these)\n\n')
        open(f, 'a').write(f'- {text}\n')
    return len(guide_rules())

def del_rule(i):
    with locked():
        f = os.path.join(ST, 'guide.md'); lines = open(f).read().splitlines(); n = -1
        for k, l in enumerate(lines):
            if l.startswith('- '):
                n += 1
                if n == i: lines.pop(k); break
        open(f, 'w').write('\n'.join(lines) + '\n')

# ---------- board columns + overlap check (added by the schedule setup, 2026-10-22) ----------
TIGHT_JS = """<script>
(function(){
 var bad=[];document.querySelectorAll('.hd,.row,.li').forEach(function(r){var prev=null;
  Array.prototype.forEach.call(r.children,function(c){var g=document.createRange();g.selectNodeContents(c);var b=g.getBoundingClientRect();
   if(!b.width)return;if(prev&&b.left<prev.right+12)bad.push('tight:'+c.textContent.trim().slice(0,24));prev=b})});
 if(bad.length){var m=document.querySelector('meta[name=fitcheck]');m.content=(m.content=='ok'?'':m.content+' | ')+bad.slice(0,4).join(' | ')}
})();
</script>"""

def board_layout_patch():
    """Boards: the 4th column holds total ROI (up to +4,524.28%), so it gets 220px; and the render fails when two cells in a
    row come closer than 12px (right-aligned text overflows to the left, which the first check can't see)."""
    global CSS, FIT_JS
    if '/*board-cols*/' in CSS: return
    CSS += '/*board-cols*/.hd,.row{grid-template-columns:52px 1fr 210px 220px 150px}\n'
    FIT_JS += TIGHT_JS
board_layout_patch()
