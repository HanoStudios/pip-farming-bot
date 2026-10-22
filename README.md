# PIP Farming bot

Runs the **PIP Farming Results** Telegram channel ([@pipfarmingresults](https://t.me/pipfarmingresults)) through the bot **@PipFarmingResultsBot**.

It turns the live Levels Socials provider stats into clean posts and sends every post to the team for approval first. Nothing goes out without someone tapping **Approve**.

## What it does

- **3 posts a day** (Bangkok time):
  - 1 pm: Highlight
  - 5 pm: Profits
  - 8 pm: Results board
- **Every Sunday at 8 pm:** the weekly results.
- **Launch week (23 to 29 Oct):** an extra 9 am launch post each day.
- **Previews 30 minutes early:** each post is sent to Samanway and Johannes privately with three buttons:
  - **Approve**: it posts.
  - **Change**: write what to change, and a new version comes back. It needs approval again.
  - **Reject**: you can make your own post instead.
- **Talk to it like a person:** in a DM, ask for a post ("make one about Cool Wave's win rate"). It makes the post and sends it for approval.
- **Teach it:** every rule you give it, and every change you ask for, is saved in `state/guide.md` and used for every new post.
- **Real numbers only:** numbers come only from the live provider API, and any caption with a number that isn't in the data is rejected. Every image carries the risk disclaimer.

## Files

| File | What it is |
|---|---|
| `pipcore.py` | The engine: provider data, post angles, captions (Claude), image rendering (Chrome), Telegram |
| `autopost.py` | The schedule: `prep` sends the previews, `post` publishes approved posts. `--dry` renders every format into `posts/preview/` |
| `bot.py` | The Telegram control bot (menu, approvals, changes, Teach, human-style chat) |
| `data/fetch-providers.sh` | Gets the live provider stats through the allowlisted server |
| `install.sh` | Sets up the 3 background jobs (launchd) on a Mac |
| `tests/test_bot.py` | The whole flow against a fake Telegram, without touching the real channel |
| `brand/` | Logo and channel avatar |
| `docs/` | "How the channel runs itself" PDF |

## Set it up on a Mac

1. **Install what it needs.** You need Python 3, Google Chrome and the Claude Code CLI, logged in once:
   ```bash
   brew install python
   npm i -g @anthropic-ai/claude-code
   claude
   ```
   The last command opens Claude so you can log in.
2. **Get the code.**
   ```bash
   git clone https://github.com/HanoStudios/pip-farming-bot.git ~/pip-farming
   cd ~/pip-farming
   ```
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
   Then send `/start` to @PipFarmingResultsBot.

**Run it on one Mac only.** Two copies would answer the bot twice and post twice. Stop the old one first:
```bash
launchctl bootout gui/$(id -u)/com.pipfarming.bot
launchctl bootout gui/$(id -u)/com.pipfarming.prep
launchctl bootout gui/$(id -u)/com.pipfarming.post
```

## Useful commands

```bash
tail -f logs/bot.log                       # what the bot is doing
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
