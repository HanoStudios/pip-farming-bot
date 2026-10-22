# PIP Farming bot

Runs the **PIP Farming Results** Telegram channel ([@pipfarmingresults](https://t.me/pipfarmingresults)) through the bot **@PipFarmingResultsBot**.

It turns the live Levels Socials provider stats into number-first posts and sends every post to the team for approval first. Nothing goes out without someone tapping **Approve**.

It only ever shows the 5 strategies we promote: **Cool Wave, Glade Coms, Nightshift FX, PBS FX, Yield Engine**, each with its own logo.

## What it does

- **3 posts a day** (Johannes' plan, Bangkok time):
  - 1 pm: **Creative.** A different design every day, from 10 formats in rotation (each one comes back only every 10 days, with a new strategy or new numbers):
    🔥 On fire (the day's best) · 🏁 The race · 🎯 93 out of 100 (win rate as 100 trades) · 🥊 Head to head · 🏆 The podium · 🛡️ The calm one (drawdown gauge) ·
    🃏 Pick your card · 🎉 Milestone club (+900% club) · 📟 The board (LED ticker) · 🤔 Guess who?
  - 5 pm: **Performance update.** One strategy in Akhil's stat-grid style: today, this week, total ROI, win rate, max drawdown, plus the day's ranking.
  - 8 pm: **Daily results.** All 5 strategies compared for the day in one table, with the best result, how many are in profit, and the average.
- **Every Sunday at 8 pm:** the weekly results, the same table for the week.
- **Results of the day:** the change in each strategy's total ROI since the last numbers of the day before (saved every time the bot reads the data). Until there is a day before, and on days the market is closed, posts use the all-time numbers.
- **Captions** are written by the code in the team's style: a short line with an emoji, the numbers, and "Not financial advice." Claude only writes when someone adds notes to a post or asks for a change.
- **Previews 30 minutes early:** each post is sent to Samanway and Johannes privately with three buttons:
  - **Approve**: it posts.
  - **Change**: write what to change, and a new version comes back. It needs approval again.
  - **Reject**: you can make your own post instead.
- **Talk to it like a person:** in a DM, ask for a post ("make one about Cool Wave's win rate"). It makes the post and sends it for approval.
- **Teach it:** every rule you give it, and every change you ask for, is saved in `state/guide.md` and used for every new post.
- **Real numbers only:** numbers come only from the live provider API, and any caption with a number that isn't in the data is rejected. Results are shown in percent. Every image carries the risk disclaimer.
- **Never in a post:** where the numbers come from ("provider API", "straight from"), "posted every day", "no edits", or any date, time or place. Captions that mention them are rejected.

## Files

| File | What it is |
|---|---|
| `pipcore.py` | The engine: provider data, day and week results, post formats, captions, image rendering (Chrome), Telegram |
| `autopost.py` | The schedule: `prep` sends the previews, `post` publishes approved posts. `--dry` renders every format into `posts/preview/` |
| `bot.py` | The Telegram control bot (menu, approvals, changes, Teach, human-style chat) |
| `data/fetch-providers.sh` | Gets the live provider stats through the allowlisted server |
| `install.sh` | Sets up the 3 background jobs (launchd) on a Mac |
| `tests/test_bot.py` | The whole flow against a fake Telegram, without touching the real channel |
| `brand/` | PIP Farming logo and avatar, `providers/` the 5 strategy logos (cut out of Akhil's story covers), `art/` background artwork from Akhil's slides |
| `docs/` | "How the channel runs itself" PDF |

## Set it up on a Mac

1. **Install what it needs.** You need Python 3 (the one that comes with macOS works), Google Chrome and the Claude Code CLI, logged in once:
   ```bash
   npm i -g @anthropic-ai/claude-code
   claude
   ```
   The last command opens Claude so you can log in. If npm says it has no permission, install it for your user only:
   `npm i -g --prefix ~/.local/node @anthropic-ai/claude-code` (the bot looks there too).
2. **Get the code.**
   ```bash
   git clone https://github.com/HanoStudios/pip-farming-bot.git ~/pip-farming
   cd ~/pip-farming
   ```
   Keep it in `~/pip-farming`. macOS does not let background jobs open files on the Desktop or in Documents, so the bot would not start from there.
3. **Add the secrets.** These come from the encrypted file sent on Telegram.
   - Put `.env` in this folder.
   - Put `LevelsNew2026.pem` in `~/.ssh/`, then run:
     ```bash
     chmod 400 ~/.ssh/LevelsNew2026.pem
     ```
   - In `.env`, set `PIP_SSH_KEY` to the full path of that key.
4. **Test the data.**
   ```bash
   ./data/fetch-providers.sh
   ```
   It should print `ok 8 providers as of ...`.
5. **Preview without posting.**
   ```bash
   python3 autopost.py --dry
   ```
   Then open `posts/preview/`.
6. **Start it.**
   ```bash
   ./install.sh
   ```
   It converts the Bangkok post times to this Mac's own time zone (run it again if the Mac's time zone changes). Then send `/start` to @PipFarmingResultsBot.

**Run it on one Mac only.** Two copies would answer the bot twice and post twice. Stop the old one first:
```bash
launchctl bootout gui/$(id -u)/com.pipfarming.bot
launchctl bootout gui/$(id -u)/com.pipfarming.prep
launchctl bootout gui/$(id -u)/com.pipfarming.post
```

## Useful commands

```bash
tail -f logs/bot.log                       # what the bot is doing ("Conflict" here = another copy is running)
python3 autopost.py --dry                  # render every post format, no posting
python3 tests/test_bot.py                  # full test on a fake Telegram
launchctl kickstart -k gui/$(id -u)/com.pipfarming.bot   # restart the bot
```

## Settings (`.env`)

See `.env.example`.

- `TELEGRAM_BOT_TOKEN`: the bot token.
- `PIP_ONLY_IDS`: who may use the bot and get previews.
- `PIP_CHAT_IDS`: who the bot chats with like an assistant.
- `PIP_SSH_KEY` and `PIP_SSH_HOST`: the server that calls the provider API. It's allowlisted by the Levels dev team, and the integration key lives on that server in `~/.levels-provider-key`.

Trading involves risk. Past performance does not guarantee future results.
