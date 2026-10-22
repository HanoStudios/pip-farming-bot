#!/usr/bin/env python3
"""PIP Farming channel autoposter. Live provider stats -> clean image + one short line -> @pipfarmingresults.
usage: post.py results|hype [--dry]   (--dry renders and prints, sends nothing)"""
import json, os, sys, subprocess, datetime, html, random, urllib.request, urllib.parse, uuid
HOME = os.path.expanduser('~'); D = os.path.join(HOME, 'pip-farming'); ST = os.path.join(D, 'state')
CHANNEL = '-1003897919358'
FETCH = os.path.join(HOME, 'Desktop/Levels Socials Studio/statics/data/fetch-providers.sh')
DATA = os.path.join(HOME, 'Desktop/Levels Socials Studio/statics/data/providers-api.json')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
TOKEN = open(os.path.join(D, '.env')).read().split('TELEGRAM_BOT_TOKEN=')[1].split()[0]
mode = sys.argv[1] if len(sys.argv) > 1 else 'results'; dry = '--dry' in sys.argv
def log(*a):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] {mode}: " + ' '.join(map(str, a))
    print(line); open(os.path.join(D, 'logs', 'post.log'), 'a').write(line + '\n')

def providers():
    if subprocess.run([FETCH], capture_output=True).returncode != 0: log('API fetch failed, using last file')
    d = json.load(open(DATA))['data']
    age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(d['asOf'].replace('Z', '+00:00'))
    if age.total_seconds() > 36 * 3600: raise SystemExit(log('data older than 36 h, not posting') or 1)
    return [p for p in d['items'] if p['pnl30d'] > 0]   # pnl30d 0 = not trading (e.g. Golden Shield)

CSS = """body{margin:0;width:1080px;height:1350px;font-family:-apple-system,"Helvetica Neue",Arial;color:#fff;background:radial-gradient(120% 70% at 50% 0%,#1b4fd6 0%,#0b2a8a 30%,#06144a 60%,#030822 100%);position:relative;overflow:hidden}
.wrap{position:relative;padding:64px 72px}.logo{height:64px}.date{position:absolute;top:80px;right:72px;font-size:22px;color:#a9bdf5}
.k{margin-top:50px;font-size:26px;letter-spacing:6px;color:#8fb0ff;font-weight:600}h1{margin:14px 0 0;font-size:80px;line-height:1.02;font-weight:800;letter-spacing:-2px}
.sub{margin-top:18px;font-size:32px;color:#c9d6ff}.foot{position:absolute;bottom:54px;left:72px;right:72px;font-size:17px;color:#7f93c9}
.tbl{margin-top:40px;background:rgba(10,30,100,.45);border:1px solid rgba(120,160,255,.25);border-radius:28px;padding:18px 30px}
.hd,.row{display:grid;grid-template-columns:56px 1fr 230px 160px 150px;align-items:center}.hd{font-size:20px;letter-spacing:3px;color:#7f9be6;padding:14px 0;border-bottom:1px solid rgba(120,160,255,.2)}
.row{padding:14px 0;border-bottom:1px solid rgba(120,160,255,.12);font-size:30px}.row:last-child{border-bottom:0}.rk{color:#7f9be6;font-weight:700}
.nm{font-weight:700}.nm span{display:block;font-size:18px;color:#8fa6e8;font-weight:500;margin-top:4px}.roi{color:#4dff9a;font-weight:800;text-align:right}.wr,.dd{text-align:right}.dd{color:#ff8a8a}.hd div:nth-child(n+3){text-align:right}
.big{margin-top:70px;font-size:190px;font-weight:900;letter-spacing:-6px;color:#4dff9a;line-height:1}.cards{margin-top:60px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:22px}
.c{background:rgba(10,30,100,.45);border:1px solid rgba(120,160,255,.25);border-radius:24px;padding:28px}.c b{display:block;font-size:44px}.c span{font-size:19px;letter-spacing:2px;color:#8fa6e8}"""
name = lambda p: html.escape(p['name'].replace(' - ', ' '))
def page(body):
    day = datetime.date.today().strftime('%d %b %Y')
    return f'<html><head><meta charset="utf-8"><style>{CSS}</style></head><body><div class="wrap"><img class="logo" src="file://{D}/brand/logo-full.png"><div class="date">{day}</div>{body}</div><div class="foot"><b>Disclaimer:</b> Trading involves risk. You may lose your invested capital. Past performance does not guarantee future results.</div></body></html>'

def results(ps):
    ps = sorted(ps, key=lambda p: -p['allTimeRoi'])[:7]
    rows = ''.join(f'<div class="row"><div class="rk">{i+1}</div><div class="nm">{name(p)}<span>{html.escape(p["strategyType"])}</span></div><div class="roi">+{p["allTimeRoi"]:,.2f}%</div><div class="wr">{p["winRate"]:.2f}%</div><div class="dd">-{p["drawdown"]:.2f}%</div></div>' for i, p in enumerate(ps))
    body = f'<div class="k">STRATEGY INDEX</div><h1>The numbers.<br>Nothing else.</h1><div class="sub">Every strategy we copy, live from Levels Socials.</div><div class="tbl"><div class="hd"><div>#</div><div>STRATEGY</div><div>TOTAL ROI</div><div>WIN RATE</div><div>MAX DD</div></div>{rows}</div>'
    caption = random.choice(['Today\'s board. Same rules: live numbers, no edits.', 'Updated. Check who moved.', 'The board, as of today.', 'Daily index. Numbers only.'])
    return page(body), caption

def hype(ps):
    picks = [
        (max(ps, key=lambda p: p['winRate']), lambda p: (f'{p["winRate"]:.2f}%', 'WIN RATE', f'{name(p)} wins most of its trades.', f'{p["winRate"]:.0f} out of 100 trades closed green. {p["name"]}.')),
        (max(ps, key=lambda p: p['pnl30d']), lambda p: (f'+{p["allTimeRoi"]:,.0f}%', 'TOTAL ROI', f'{name(p)} had the strongest month.', f'Strongest last 30 days: {p["name"]}.')),
        (min(ps, key=lambda p: p['drawdown']), lambda p: (f'-{p["drawdown"]:.1f}%', 'MAX DRAWDOWN', f'{name(p)} keeps it calm.', f'Lowest drawdown on the board. {p["name"]}.')),
        (max(ps, key=lambda p: p['totalTrades']), lambda p: (f'{p["totalTrades"]:,}', 'TRADES CLOSED', f'{name(p)} never stops.', f'{p["totalTrades"]:,} trades and counting. {p["name"]}.')),
    ]
    used = json.load(open(os.path.join(ST, 'hype.json'))) if os.path.exists(os.path.join(ST, 'hype.json')) else []
    i = next((k for k in range(len(picks)) if k not in used[-3:]), 0)
    json.dump((used + [i])[-12:], open(os.path.join(ST, 'hype.json'), 'w'))
    p, f = picks[i]; big, label, head, caption = f(p)
    cards = f'<div class="c"><b>+{p["allTimeRoi"]:,.0f}%</b><span>TOTAL ROI</span></div><div class="c"><b>{p["winRate"]:.1f}%</b><span>WIN RATE</span></div><div class="c"><b>-{p["drawdown"]:.1f}%</b><span>MAX DD</span></div>'
    body = f'<div class="k">{label}</div><div class="big">{big}</div><h1 style="font-size:64px;margin-top:40px">{head}</h1><div class="sub">{html.escape(p["strategyType"])} strategy · {p["followers"]:,} copying</div><div class="cards">{cards}</div>'
    return page(body), caption

def render(doc):
    f = os.path.join(ST, f'{mode}.html'); png = os.path.join(D, 'posts', f'{datetime.date.today()}-{mode}.png')
    open(f, 'w').write(doc)
    subprocess.run([CHROME, '--headless', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files', '--window-size=1080,1350', '--force-device-scale-factor=2', f'--screenshot={png}', f'file://{f}'], capture_output=True, timeout=120)
    if not os.path.exists(png): raise SystemExit(log('render failed') or 1)
    return png

def send(png, caption):
    b = uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{CHANNEL}\r\n--{b}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="post.png"\r\nContent-Type: image/png\r\n\r\n').encode() + open(png, 'rb').read() + f'\r\n--{b}--\r\n'.encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{TOKEN}/sendPhoto', body, {'Content-Type': f'multipart/form-data; boundary={b}'}), timeout=120))
    return r['result']['message_id']

ps = providers()
doc, caption = (hype if mode == 'hype' else results)(ps)
png = render(doc)
if dry: log('dry run:', png, '|', caption)
else: log('sent', send(png, caption), '|', caption)
