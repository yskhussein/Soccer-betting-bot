"""
soccer_bot.py — AI Soccer Betting Bot
Covers all leagues worldwide | All bet types | Paper + Real money tracking
Deploy on Railway as a separate project
"""
import os
import json
import asyncio
import logging
import threading
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string, request, Response
from groq import Groq
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── ENV VARS ──────────────────────────────────────────────────────────────────
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ADMIN_CHAT_ID      = os.environ["ADMIN_CHAT_ID"]
API_FOOTBALL_KEY   = os.environ["API_FOOTBALL_KEY"]   # from api-football.com
STATE_FILE         = "/app/soccer_state.json"

# ── CONFIG ────────────────────────────────────────────────────────────────────
PAPER_BANKROLL     = 500.0
SCAN_INTERVAL      = 3600        # check for matches every hour
TIP_HOUR           = 9           # 9am UTC daily tips

# Top league IDs on API-Football
LEAGUE_IDS = {
    "Premier League":    39,
    "La Liga":          140,
    "Serie A":          135,
    "Bundesliga":        78,
    "Champions League":   2,
    "Ligue 1":          61,
    "Eredivisie":        88,
    "Primeira Liga":     94,
    "Super Lig":        203,
    "MLS":             253,
    "Brasileirao":      71,
    "Argentine Liga":  128,
    "Saudi Pro League": 307,
    "J1 League":       292,
    "Europa League":     3,
    "Conference League": 848,
}

# ── STATE ─────────────────────────────────────────────────────────────────────
state = {
    "paper_bankroll":    PAPER_BANKROLL,
    "real_bankroll":     0.0,       # user sets this
    "paper_bets":        [],        # all paper bets placed
    "real_bets":         [],        # all real bets placed
    "bet_history":       [],        # settled bets
    "tips_today":        [],        # today's tips
    "stats": {
        "paper": {"total": 0, "won": 0, "lost": 0, "void": 0, "profit": 0.0, "roi": 0.0},
        "real":  {"total": 0, "won": 0, "lost": 0, "void": 0, "profit": 0.0, "roi": 0.0},
    },
    "last_scan":         None,
    "start_time":        datetime.now(timezone.utc).isoformat(),
    "activity_log":      [],
    "paused":            False,
    "groq_pause_until":  0,
}

groq_client   = None
telegram_bot  = None
_bot_loop     = None   # event loop of the telegram thread
flask_app     = Flask(__name__)


def get_groq():
    import httpx
    return Groq(api_key=GROQ_API_KEY, http_client=httpx.Client())


# ══════════════════════════════════════════════════════════════════════════════
# PERSIST STATE
# ══════════════════════════════════════════════════════════════════════════════

SAVE_KEYS = ["paper_bankroll","real_bankroll","paper_bets","real_bets",
             "bet_history","stats","tips_today","paused"]

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({k: state[k] for k in SAVE_KEYS}, f)
        logger.info("State saved")
    except Exception as e:
        logger.warning(f"Save failed: {e}")

def load_state():
    try:
        if not os.path.exists(STATE_FILE):
            logger.info("No saved state — starting fresh")
            return
        with open(STATE_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in state:
                state[k] = v
        logger.info(f"State loaded — paper: ${state['paper_bankroll']:.2f} | bets: {len(state['bet_history'])}")
    except Exception as e:
        logger.warning(f"Load failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# API-FOOTBALL DATA
# ══════════════════════════════════════════════════════════════════════════════

def api_football(endpoint: str, params: dict) -> dict:
    """Call API-Football v3."""
    try:
        r = requests.get(
            f"https://v3.football.api-sports.io/{endpoint}",
            headers={"x-apisports-key": API_FOOTBALL_KEY},
            params=params,
            timeout=15,
        )
        return r.json()
    except Exception as e:
        logger.error(f"API-Football error ({endpoint}): {e}")
        return {}


def get_todays_fixtures() -> list:
    """Get all fixtures for today across all tracked leagues."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_fixtures = []
    for league_name, league_id in LEAGUE_IDS.items():
        data = api_football("fixtures", {"league": league_id, "date": today, "season": 2024})
        fixtures = data.get("response", [])
        for f in fixtures:
            fixture_id = f["fixture"]["id"]
            home       = f["teams"]["home"]["name"]
            away       = f["teams"]["away"]["name"]
            kickoff    = f["fixture"]["date"]
            status     = f["fixture"]["status"]["short"]
            venue      = f["fixture"].get("venue", {}).get("name", "Unknown")
            all_fixtures.append({
                "fixture_id": fixture_id,
                "league":     league_name,
                "league_id":  league_id,
                "home":       home,
                "away":       away,
                "kickoff":    kickoff,
                "status":     status,
                "venue":      venue,
            })
        if fixtures:
            logger.info(f"Found {len(fixtures)} fixtures in {league_name}")
    return all_fixtures


def get_team_stats(team_id: int, league_id: int) -> dict:
    """Get team statistics for current season."""
    data = api_football("teams/statistics", {
        "team": team_id, "league": league_id, "season": 2024
    })
    resp = data.get("response", {})
    if not resp:
        return {}
    fixtures     = resp.get("fixtures", {})
    goals        = resp.get("goals", {})
    form_str     = resp.get("form", "") or ""
    clean_sheets = resp.get("clean_sheet", {})
    return {
        "played":       fixtures.get("played", {}).get("total", 0),
        "wins":         fixtures.get("wins", {}).get("total", 0),
        "draws":        fixtures.get("draws", {}).get("total", 0),
        "losses":       fixtures.get("loses", {}).get("total", 0),
        "goals_for":    goals.get("for", {}).get("total", {}).get("total", 0),
        "goals_against":goals.get("against", {}).get("total", {}).get("total", 0),
        "clean_sheets": clean_sheets.get("total", 0),
        "form":         form_str[-5:] if form_str else "?????",
        "home_wins":    fixtures.get("wins", {}).get("home", 0),
        "away_wins":    fixtures.get("wins", {}).get("away", 0),
    }


def get_h2h(home_id: int, away_id: int) -> list:
    """Get head to head results."""
    data = api_football("fixtures/headtohead", {
        "h2h": f"{home_id}-{away_id}", "last": 10
    })
    results = []
    for f in data.get("response", [])[:5]:
        hg = f["goals"]["home"]
        ag = f["goals"]["away"]
        ht = f["teams"]["home"]["name"]
        at = f["teams"]["away"]["name"]
        results.append(f"{ht} {hg}-{ag} {at}")
    return results


def get_odds(fixture_id: int) -> dict:
    """Get live odds for a fixture."""
    data = api_football("odds", {"fixture": fixture_id, "bookmaker": 6})  # Bet365
    odds_out = {}
    for resp in data.get("response", []):
        for bookie in resp.get("bookmakers", []):
            for bet in bookie.get("bets", []):
                name = bet["name"]
                if name in ("Match Winner", "Goals Over/Under", "Both Teams Score",
                            "Exact Score", "Asian Handicap"):
                    odds_out[name] = {v["value"]: v["odd"] for v in bet.get("values", [])}
    return odds_out


def get_injuries(team_id: int) -> list:
    """Get current injuries for a team."""
    data = api_football("injuries", {"team": team_id, "season": 2024})
    injured = []
    for p in data.get("response", [])[:5]:
        name   = p["player"]["name"]
        reason = p["player"].get("reason", "Injured")
        injured.append(f"{name} ({reason})")
    return injured


def get_fixture_teams(fixture_id: int) -> tuple:
    """Get team IDs from a fixture."""
    data = api_football("fixtures", {"id": fixture_id})
    resp = data.get("response", [])
    if not resp:
        return None, None
    home_id = resp[0]["teams"]["home"]["id"]
    away_id = resp[0]["teams"]["away"]["id"]
    return home_id, away_id


def get_news(home: str, away: str) -> str:
    """Fetch Google News for the match."""
    try:
        query = f"{home} vs {away} football".replace(" ", "+")
        feed  = feedparser.parse(
            f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        )
        headlines = [e.title for e in feed.entries[:3]]
        return " | ".join(headlines) if headlines else "No news found"
    except:
        return "No news found"


# ══════════════════════════════════════════════════════════════════════════════
# AI PREDICTION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def ai_predict(fixture: dict, home_stats: dict, away_stats: dict,
               h2h: list, odds: dict, home_injuries: list,
               away_injuries: list, news: str) -> dict:
    """Ask Groq AI to predict the match and suggest bets."""

    # Check rate limit
    if state.get("groq_pause_until", 0) > datetime.now(timezone.utc).timestamp():
        remaining = int((state["groq_pause_until"] - datetime.now(timezone.utc).timestamp()) / 60)
        logger.info(f"Groq paused {remaining}m — skipping {fixture['home']} vs {fixture['away']}")
        return None

    system_prompt = """You are an elite soccer betting analyst AI with deep knowledge of football statistics, betting markets, and value identification.

Analyze the match data and identify ALL value bets across multiple markets. You think like a professional bettor who focuses on Expected Value (EV), not just picking winners.

Respond ONLY with valid JSON, no markdown:
{
  "match_summary": "<2 sentence overview of the match>",
  "predicted_score": "<e.g. 2-1>",
  "predicted_winner": "home" | "away" | "draw",
  "win_probability": {"home": <0-100>, "draw": <0-100>, "away": <0-100>},
  "bets": [
    {
      "market": "<e.g. Over 2.5 Goals>",
      "bet_type": "1X2" | "over_under" | "btts" | "correct_score" | "asian_handicap",
      "selection": "<exact selection>",
      "odds": <float>,
      "confidence": <0-100>,
      "stake_pct": <float 0.5-5.0>,
      "reasoning": "<specific reasoning for this bet>",
      "key_stat": "<most important stat supporting this bet>",
      "risk": "low" | "medium" | "high"
    }
  ],
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "avoid_reason": "<null or reason to skip this match entirely>",
  "value_rating": <1-10>
}

BETTING RULES:
- Only include bets where you see genuine value (odds higher than true probability)
- Set confidence and stake_pct based on strength of evidence
- Include 1-5 bets per match across different markets
- If match has no value, return empty bets array and explain in avoid_reason
- Consider injuries, form, h2h, home advantage, and odds movement
- Low risk: stake 1-2% | Medium: 2-3% | High: 3-5%"""

    def fmt_stats(stats: dict, label: str) -> str:
        if not stats:
            return f"{label}: No data"
        return (f"{label}: P{stats.get('played',0)} W{stats.get('wins',0)} "
                f"D{stats.get('draws',0)} L{stats.get('losses',0)} "
                f"GF{stats.get('goals_for',0)} GA{stats.get('goals_against',0)} "
                f"Form:{stats.get('form','?')} CS:{stats.get('clean_sheets',0)}")

    odds_str = ""
    for market, values in odds.items():
        odds_str += f"\n  {market}: " + " | ".join([f"{k}@{v}" for k,v in list(values.items())[:4]])

    user_msg = f"""MATCH: {fixture['home']} vs {fixture['away']}
League: {fixture['league']} | Kickoff: {fixture['kickoff']}
Venue: {fixture.get('venue','Unknown')}

TEAM STATS:
{fmt_stats(home_stats, fixture['home'])}
{fmt_stats(away_stats, fixture['away'])}

HEAD TO HEAD (last 5):
{chr(10).join(h2h) if h2h else 'No H2H data'}

INJURIES:
{fixture['home']}: {', '.join(home_injuries) if home_injuries else 'None reported'}
{fixture['away']}: {', '.join(away_injuries) if away_injuries else 'None reported'}

CURRENT ODDS:{odds_str if odds_str else chr(10)+'  No odds available'}

LATEST NEWS: {news}

Analyze and provide value bets. JSON only:"""

    try:
        resp = get_groq().chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json","").replace("```","").strip()
        import re as _re
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if m:
            raw = m.group(0)
        return json.loads(raw)
    except Exception as e:
        err = str(e)
        if "429" in err or "rate_limit" in err.lower():
            import re as _re
            wm = _re.search(r'try again in (\d+)m', err)
            wait = int(wm.group(1)) + 1 if wm else 6
            state["groq_pause_until"] = datetime.now(timezone.utc).timestamp() + wait * 60
            logger.warning(f"Groq rate limited — pausing {wait}m")
        else:
            logger.error(f"AI prediction error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# BET TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def place_paper_bet(fixture: dict, bet: dict, prediction: dict) -> dict:
    """Record a paper bet."""
    stake = round(state["paper_bankroll"] * (bet.get("stake_pct", 2.0) / 100), 2)
    stake = max(0.50, min(stake, state["paper_bankroll"] * 0.05))
    record = {
        "id":           f"P{len(state['paper_bets'])+1:04d}",
        "type":         "paper",
        "fixture_id":   fixture["fixture_id"],
        "match":        f"{fixture['home']} vs {fixture['away']}",
        "league":       fixture["league"],
        "kickoff":      fixture["kickoff"],
        "market":       bet["market"],
        "selection":    bet["selection"],
        "odds":         float(bet.get("odds", 2.0)),
        "stake":        stake,
        "potential_win":round(stake * float(bet.get("odds", 2.0)), 2),
        "confidence":   bet.get("confidence", 0),
        "risk":         bet.get("risk", "medium"),
        "reasoning":    bet.get("reasoning", ""),
        "key_stat":     bet.get("key_stat", ""),
        "status":       "pending",
        "placed_at":    datetime.now(timezone.utc).isoformat(),
        "predicted_score": prediction.get("predicted_score",""),
        "result":       None,
        "profit":       None,
    }
    state["paper_bets"].append(record)
    state["paper_bankroll"] -= stake
    state["stats"]["paper"]["total"] += 1
    return record


def place_real_bet(fixture: dict, bet: dict, prediction: dict) -> dict:
    """Record a real money bet (manual placement by user)."""
    stake = round(state["real_bankroll"] * (bet.get("stake_pct", 2.0) / 100), 2) if state["real_bankroll"] > 0 else 0
    record = {
        "id":           f"R{len(state['real_bets'])+1:04d}",
        "type":         "real",
        "fixture_id":   fixture["fixture_id"],
        "match":        f"{fixture['home']} vs {fixture['away']}",
        "league":       fixture["league"],
        "kickoff":      fixture["kickoff"],
        "market":       bet["market"],
        "selection":    bet["selection"],
        "odds":         float(bet.get("odds", 2.0)),
        "stake":        stake,
        "potential_win":round(stake * float(bet.get("odds", 2.0)), 2),
        "confidence":   bet.get("confidence", 0),
        "risk":         bet.get("risk", "medium"),
        "reasoning":    bet.get("reasoning", ""),
        "status":       "pending",
        "placed_at":    datetime.now(timezone.utc).isoformat(),
        "result":       None,
        "profit":       None,
    }
    state["real_bets"].append(record)
    return record


def settle_bets():
    """Check pending bets and settle finished matches."""
    pending = [b for b in state["paper_bets"] + state["real_bets"] if b["status"] == "pending"]
    if not pending:
        return
    settled = 0
    for bet in pending:
        try:
            data    = api_football("fixtures", {"id": bet["fixture_id"]})
            resp    = data.get("response", [])
            if not resp:
                continue
            fixture = resp[0]
            status  = fixture["fixture"]["status"]["short"]
            if status not in ("FT", "AET", "PEN"):
                continue   # not finished yet
            hg = fixture["goals"]["home"] or 0
            ag = fixture["goals"]["away"] or 0
            result = determine_result(bet, hg, ag, fixture)
            bet["status"] = result["status"]
            bet["result"] = f"{hg}-{ag}"
            bet["profit"] = result["profit"]
            # Update bankroll
            if bet["type"] == "paper":
                state["paper_bankroll"] += bet["stake"] + result["profit"]
                stats = state["stats"]["paper"]
            else:
                state["real_bankroll"]  += bet["stake"] + result["profit"]
                stats = state["stats"]["real"]
            if result["status"] == "won":
                stats["won"]    += 1
            elif result["status"] == "lost":
                stats["lost"]   += 1
            else:
                stats["void"]   += 1
            stats["profit"] += result["profit"]
            total = stats["won"] + stats["lost"]
            staked = sum(b["stake"] for b in
                         (state["paper_bets"] if bet["type"]=="paper" else state["real_bets"])
                         if b["status"] != "pending")
            stats["roi"] = round((stats["profit"] / staked * 100) if staked > 0 else 0, 2)
            state["bet_history"].insert(0, {**bet})
            settled += 1
        except Exception as e:
            logger.error(f"Settle error for bet {bet['id']}: {e}")
    if settled:
        save_state()
        logger.info(f"Settled {settled} bets")


def determine_result(bet: dict, hg: int, ag: int, fixture: dict) -> dict:
    """Determine if a bet won or lost."""
    sel = bet["selection"].lower().strip()
    bt  = bet.get("market","").lower()
    total_goals = hg + ag
    profit = 0.0
    status = "void"

    try:
        if "1x2" in bt or "match winner" in bt or "winner" in bt:
            if sel in ("home","1") and hg > ag:
                status = "won"; profit = round(bet["stake"] * (bet["odds"] - 1), 2)
            elif sel in ("draw","x") and hg == ag:
                status = "won"; profit = round(bet["stake"] * (bet["odds"] - 1), 2)
            elif sel in ("away","2") and ag > hg:
                status = "won"; profit = round(bet["stake"] * (bet["odds"] - 1), 2)
            else:
                status = "lost"; profit = -bet["stake"]

        elif "over" in sel or "under" in sel:
            line = float(''.join(c for c in sel if c.isdigit() or c == '.') or "2.5")
            if "over" in sel:
                if total_goals > line:
                    status = "won"; profit = round(bet["stake"] * (bet["odds"] - 1), 2)
                else:
                    status = "lost"; profit = -bet["stake"]
            else:
                if total_goals < line:
                    status = "won"; profit = round(bet["stake"] * (bet["odds"] - 1), 2)
                else:
                    status = "lost"; profit = -bet["stake"]

        elif "btts" in bt or "both teams" in bt:
            btts = hg > 0 and ag > 0
            if ("yes" in sel and btts) or ("no" in sel and not btts):
                status = "won"; profit = round(bet["stake"] * (bet["odds"] - 1), 2)
            else:
                status = "lost"; profit = -bet["stake"]

        elif "correct score" in bt:
            parts = sel.replace(" ","").split("-")
            if len(parts) == 2 and int(parts[0]) == hg and int(parts[1]) == ag:
                status = "won"; profit = round(bet["stake"] * (bet["odds"] - 1), 2)
            else:
                status = "lost"; profit = -bet["stake"]

        elif "asian" in bt:
            # Simplified Asian Handicap
            status = "lost"; profit = -bet["stake"]

    except Exception as e:
        logger.error(f"Result determination error: {e}")
        status = "void"; profit = 0.0

    return {"status": status, "profit": profit}


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def notify_sync(msg: str):
    """Send Telegram message from any thread safely."""
    if telegram_bot is None or _bot_loop is None:
        logger.warning("Telegram not ready yet")
        return
    import asyncio as _asyncio
    async def _send():
        for attempt in range(1, 4):
            try:
                await telegram_bot.send_message(
                    chat_id=ADMIN_CHAT_ID, text=msg,
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception as e:
                logger.warning(f"Telegram notify attempt {attempt}/3: {e}")
                if attempt < 3:
                    await _asyncio.sleep(3)
    try:
        future = _asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
        future.result(timeout=15)
    except Exception as e:
        logger.error(f"notify_sync failed: {e}")

async def notify(msg: str, keyboard=None):
    """Async notify — use within bot event loop."""
    if telegram_bot is None:
        logger.warning("Telegram bot not ready")
        return
    for attempt in range(1, 4):
        try:
            await telegram_bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=msg,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.warning(f"Telegram notify attempt {attempt}/3: {e}")
            if attempt < 3:
                await asyncio.sleep(3)


def format_tip(fixture: dict, bet: dict, prediction: dict) -> str:
    risk_emoji = {"low":"🟢","medium":"🟡","high":"🔴"}.get(bet.get("risk","medium"),"⚪")
    bt_emoji   = {"1X2":"⚽","over_under":"📊","btts":"🎯","correct_score":"🔢","asian_handicap":"🏹"}.get(bet.get("bet_type",""),"💡")
    msg  = f"⚽ <b>{fixture['home']} vs {fixture['away']}</b>\n"
    msg += f"🏆 {fixture['league']} | ⏰ {fixture['kickoff'][11:16]} UTC\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{bt_emoji} <b>Market:</b> {bet['market']}\n"
    msg += f"🎯 <b>Selection:</b> {bet['selection']}\n"
    msg += f"💰 <b>Odds:</b> {bet.get('odds','N/A')}\n"
    msg += f"🤖 <b>Confidence:</b> {bet.get('confidence',0)}%\n"
    msg += f"{risk_emoji} <b>Risk:</b> {bet.get('risk','medium').capitalize()}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🧠 {bet.get('reasoning','')}\n"
    msg += f"📊 Key stat: {bet.get('key_stat','')}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Predicted score: <b>{prediction.get('predicted_score','?')}</b>\n"
    for k in (prediction.get("key_factors") or [])[:2]:
        msg += f"• {k}\n"
    return msg


def format_daily_summary(tips: list) -> str:
    if not tips:
        return "📭 <b>No betting tips for today</b>\n\nNo matches with sufficient value found."
    msg  = f"⚽ <b>TODAY'S BETTING TIPS</b> — {datetime.now(timezone.utc).strftime('%d %b %Y')}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📋 <b>{len(tips)} tips</b> across {len(set(t['league'] for t in tips))} leagues\n"
    paper_stake = sum(t.get('paper_stake',0) for t in tips)
    msg += f"💵 Total paper stake: ${paper_stake:.2f}\n\n"
    for i, tip in enumerate(tips[:10], 1):
        risk_emoji = {"low":"🟢","medium":"🟡","high":"🔴"}.get(tip.get("risk","medium"),"⚪")
        msg += f"{i}. {risk_emoji} <b>{tip['match']}</b>\n"
        msg += f"   {tip['market']} → <b>{tip['selection']}</b> @ {tip.get('odds','?')} ({tip.get('confidence',0)}%)\n\n"
    msg += f"💼 Paper bankroll: ${state['paper_bankroll']:.2f}\n"
    msg += f"📊 Paper ROI: {state['stats']['paper']['roi']:.1f}%\n"
    return msg


# ══════════════════════════════════════════════════════════════════════════════
# MAIN DAILY SCAN
# ══════════════════════════════════════════════════════════════════════════════

async def run_daily_tips():
    """Main job: fetch today's matches, analyze, send tips."""
    if state["paused"]:
        logger.info("Bot paused — skipping daily tips")
        return

    logger.info("Starting daily tips generation...")
    await notify("🔍 <b>Analyzing today's matches...</b>\nFetching fixtures and running AI predictions.")

    fixtures = get_todays_fixtures()
    if not fixtures:
        await notify("📭 <b>No matches found today</b> across tracked leagues.")
        return

    logger.info(f"Found {len(fixtures)} fixtures today")
    tips_generated = []
    state["tips_today"] = []

    for fixture in fixtures[:30]:  # cap at 30 to save API calls
        try:
            # Get team IDs
            home_id, away_id = get_fixture_teams(fixture["fixture_id"])
            if not home_id:
                continue

            # Gather all data in parallel (sequential to avoid rate limits)
            home_stats    = get_team_stats(home_id, fixture["league_id"])
            away_stats    = get_team_stats(away_id, fixture["league_id"])
            h2h           = get_h2h(home_id, away_id)
            odds          = get_odds(fixture["fixture_id"])
            home_injuries = get_injuries(home_id)
            away_injuries = get_injuries(away_id)
            news          = get_news(fixture["home"], fixture["away"])

            # AI prediction
            prediction = ai_predict(
                fixture, home_stats, away_stats, h2h, odds,
                home_injuries, away_injuries, news
            )
            if not prediction:
                continue

            # Skip if AI says no value
            if prediction.get("avoid_reason") and not prediction.get("bets"):
                logger.info(f"Skipping {fixture['home']} vs {fixture['away']}: {prediction['avoid_reason']}")
                continue

            # Process each bet
            for bet in prediction.get("bets", []):
                if not bet.get("selection"):
                    continue

                # Paper bet
                paper_record = place_paper_bet(fixture, bet, prediction)

                # Real bet (only if bankroll set)
                real_record = None
                if state["real_bankroll"] > 10:
                    real_record = place_real_bet(fixture, bet, prediction)

                tip = {
                    "match":        f"{fixture['home']} vs {fixture['away']}",
                    "league":       fixture["league"],
                    "kickoff":      fixture["kickoff"],
                    "market":       bet["market"],
                    "selection":    bet["selection"],
                    "odds":         bet.get("odds", 2.0),
                    "confidence":   bet.get("confidence", 0),
                    "risk":         bet.get("risk", "medium"),
                    "reasoning":    bet.get("reasoning", ""),
                    "paper_stake":  paper_record["stake"],
                    "paper_id":     paper_record["id"],
                    "real_id":      real_record["id"] if real_record else None,
                    "predicted_score": prediction.get("predicted_score",""),
                    "value_rating": prediction.get("value_rating", 5),
                }
                tips_generated.append(tip)

                # Send individual tip via Telegram
                msg = format_tip(fixture, bet, prediction)
                await notify(msg)
                await asyncio.sleep(1)  # avoid flood

            await asyncio.sleep(2)  # rate limit API-Football

        except Exception as e:
            logger.error(f"Error processing {fixture.get('home','?')} vs {fixture.get('away','?')}: {e}")

    state["tips_today"] = tips_generated
    save_state()

    # Send summary
    summary = format_daily_summary(tips_generated)
    await notify(summary)

    # Log activity
    state["activity_log"].insert(0, {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": f"Generated {len(tips_generated)} tips from {len(fixtures)} fixtures",
    })
    state["activity_log"] = state["activity_log"][:50]
    logger.info(f"Daily tips complete: {len(tips_generated)} tips generated")


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ <b>Soccer Betting AI Bot</b>\n\n"
        "Commands:\n"
        "/tips — Today's betting tips\n"
        "/stats — Performance statistics\n"
        "/bets — Active pending bets\n"
        "/bankroll — Current bankroll\n"
        "/setreal [amount] — Set real money bankroll\n"
        "/settle — Manually trigger bet settlement\n"
        "/pause — Pause the bot\n"
        "/resume — Resume the bot\n"
        "/analyze — Trigger new analysis now",
        parse_mode=ParseMode.HTML,
    )

async def cmd_tips(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tips = state["tips_today"]
    if not tips:
        await update.message.reply_text("📭 No tips generated yet today. Try /analyze to run now.")
        return
    summary = format_daily_summary(tips)
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["stats"]["paper"]
    r = state["stats"]["real"]
    total_p = p["won"] + p["lost"]
    total_r = r["won"] + r["lost"]
    wr_p = round(p["won"]/total_p*100,1) if total_p > 0 else 0
    wr_r = round(r["won"]/total_r*100,1) if total_r > 0 else 0
    msg = (
        f"📊 <b>PERFORMANCE STATS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>PAPER BETTING</b>\n"
        f"💵 Bankroll: ${state['paper_bankroll']:.2f} (started $500)\n"
        f"📈 ROI: {p['roi']:.1f}%\n"
        f"✅ Won: {p['won']} | ❌ Lost: {p['lost']} | Win rate: {wr_p:.1f}%\n"
        f"💰 Total profit: ${p['profit']:+.2f}\n\n"
        f"💴 <b>REAL BETTING</b>\n"
        f"💵 Bankroll: ${state['real_bankroll']:.2f}\n"
        f"📈 ROI: {r['roi']:.1f}%\n"
        f"✅ Won: {r['won']} | ❌ Lost: {r['lost']} | Win rate: {wr_r:.1f}%\n"
        f"💰 Total profit: ${r['profit']:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🗓 Running since: {state['start_time'][:10]}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_bets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pending = [b for b in state["paper_bets"] if b["status"] == "pending"]
    if not pending:
        await update.message.reply_text("📭 No pending paper bets.")
        return
    lines = [f"📋 <b>PENDING BETS ({len(pending)})</b>\n"]
    for b in pending[:10]:
        lines.append(f"📌 {b['id']} | {b['match']}\n   {b['market']} → <b>{b['selection']}</b> @ {b['odds']} | Stake: ${b['stake']:.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_bankroll(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💵 <b>BANKROLL</b>\n"
        f"📝 Paper: ${state['paper_bankroll']:.2f}\n"
        f"💴 Real: ${state['real_bankroll']:.2f}\n"
        f"📈 Paper P&L: ${state['stats']['paper']['profit']:+.2f}\n"
        f"📈 Real P&L: ${state['stats']['real']['profit']:+.2f}",
        parse_mode=ParseMode.HTML,
    )

async def cmd_setreal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(ctx.args[0])
        state["real_bankroll"] = amount
        save_state()
        await update.message.reply_text(f"✅ Real bankroll set to ${amount:.2f}")
    except:
        await update.message.reply_text("Usage: /setreal 1000")

async def cmd_settle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Checking and settling finished bets...")
    settle_bets()
    await update.message.reply_text("✅ Settlement complete. Check /stats for updates.")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    await update.message.reply_text("⏸ Bot <b>paused</b>.", parse_mode=ParseMode.HTML)

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    await update.message.reply_text("▶️ Bot <b>resumed</b>.", parse_mode=ParseMode.HTML)

async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Running analysis now...")
    await run_daily_tips()


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

async def scheduler(app_tg):
    load_state()
    await notify(
        f"⚽ <b>Soccer Betting AI Bot Online!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Mode: Paper + Real tracking\n"
        f"💵 Paper bankroll: ${state['paper_bankroll']:.2f}\n"
        f"🏆 Leagues: {len(LEAGUE_IDS)} competitions\n"
        f"🎯 Bet types: 1X2 | O/U | BTTS | Correct Score | Asian HCP\n"
        f"⏰ Daily tips: 9am UTC\n"
        f"🌐 Dashboard: check your Railway URL\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"/tips /stats /bets /bankroll /analyze"
    )

    tips_sent_today = False
    last_settle_day = None

    while True:
        now = datetime.now(timezone.utc)
        state["last_scan"] = now.isoformat()

        # Daily tips at 9am UTC
        if now.hour == TIP_HOUR and now.minute < 5 and not tips_sent_today:
            await run_daily_tips()
            tips_sent_today = True

        # Reset daily flag
        if now.hour == 0:
            tips_sent_today = False

        # Settle bets every 2 hours
        if last_settle_day != now.day or (now.hour % 2 == 0 and now.minute < 5):
            settle_bets()
            last_settle_day = now.day

        await asyncio.sleep(SCAN_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# FLASK WEB DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>⚽ Soccer Betting AI</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{--bg0:#0a0f0a;--bg1:#0f1a0f;--bg2:#162416;--bg3:#1e301e;--border:#2a4a2a;
  --accent:#00e676;--accent2:#00a854;--yellow:#ffd600;--red:#ff1744;--blue:#00a8ff;
  --text:#c8e8c8;--text-dim:#5a8a5a;--text-bright:#e8f8e8;
  --mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg0);color:var(--text);font-family:var(--sans);min-height:100vh}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:999;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.03) 2px,rgba(0,0,0,0.03) 4px)}
.header{background:var(--bg1);border-bottom:1px solid var(--border);padding:0 24px;
  display:flex;align-items:center;justify-content:space-between;height:56px;position:sticky;top:0;z-index:100}
.logo{font-family:var(--mono);font-size:1.1rem;font-weight:600;color:var(--accent);letter-spacing:2px}
.mode-badge{background:rgba(0,230,118,0.1);border:1px solid var(--accent2);color:var(--accent);
  font-family:var(--mono);font-size:0.7rem;padding:3px 10px;border-radius:2px;letter-spacing:1px}
.header-right{display:flex;align-items:center;gap:20px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.clock{font-family:var(--mono);font-size:0.85rem;color:var(--accent)}
.last-update{font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)}
.nav{background:var(--bg1);border-bottom:1px solid var(--border);display:flex;padding:0 24px;overflow-x:auto}
.nav-tab{padding:12px 18px;font-size:0.78rem;font-weight:500;color:var(--text-dim);cursor:pointer;
  border-bottom:2px solid transparent;letter-spacing:0.5px;transition:all 0.2s;text-transform:uppercase;white-space:nowrap}
.nav-tab:hover{color:var(--text)} .nav-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.page{display:none;padding:20px 24px} .page.active{display:block}
.grid{display:grid;gap:16px}
.grid-4{grid-template-columns:repeat(4,1fr)} .grid-2{grid-template-columns:repeat(2,1fr)}
.grid-3{grid-template-columns:repeat(3,1fr)} .grid-2-1{grid-template-columns:2fr 1fr}
@media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.grid-4,.grid-3,.grid-2,.grid-2-1{grid-template-columns:1fr}}
.card{background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:16px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--accent2),var(--accent),transparent)}
.card-title{font-family:var(--mono);font-size:0.68rem;font-weight:500;color:var(--text-dim);
  letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.card-title .dot{width:6px;height:6px;border-radius:50%;background:var(--accent)}
.kpi-value{font-family:var(--mono);font-size:1.8rem;font-weight:600;color:var(--text-bright);line-height:1}
.kpi-sub{font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);margin-top:6px}
.kpi-change{font-family:var(--mono);font-size:0.85rem;margin-top:4px}
.up{color:var(--accent)} .down{color:var(--red)} .neutral{color:var(--text-dim)}
.data-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:0.78rem}
.data-table th{text-align:left;padding:8px 10px;color:var(--text-dim);font-size:0.65rem;
  letter-spacing:1px;text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--bg2)}
.data-table td{padding:9px 10px;border-bottom:1px solid rgba(42,74,42,0.5);color:var(--text)}
.data-table tr:hover td{background:rgba(0,230,118,0.04)}
.data-table .empty{text-align:center;color:var(--text-dim);padding:30px;font-size:0.8rem}
.scroll-panel{max-height:450px;overflow-y:auto}
.scroll-panel::-webkit-scrollbar{width:4px}
.scroll-panel::-webkit-scrollbar-track{background:var(--bg0)}
.scroll-panel::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.tip-card{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:14px;
  margin-bottom:10px;border-left:3px solid var(--accent)}
.tip-card.high{border-left-color:var(--red)} .tip-card.low{border-left-color:var(--accent)}
.tip-match{font-family:var(--mono);font-weight:700;font-size:0.9rem;color:var(--text-bright);margin-bottom:6px}
.tip-meta{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.badge{font-family:var(--mono);font-size:0.65rem;padding:2px 8px;border-radius:2px;letter-spacing:0.5px}
.badge.win{background:rgba(0,230,118,0.15);color:var(--accent)}
.badge.loss{background:rgba(255,23,68,0.15);color:var(--red)}
.badge.pending{background:rgba(255,214,0,0.15);color:var(--yellow)}
.badge.void{background:rgba(90,122,90,0.2);color:var(--text-dim)}
.tip-reasoning{font-size:0.78rem;color:var(--text);line-height:1.5}
.chart-wrap{position:relative;height:220px}
.btn{padding:10px 16px;border:1px solid var(--border);background:var(--bg2);color:var(--text);
  font-family:var(--mono);font-size:0.75rem;letter-spacing:1px;cursor:pointer;border-radius:3px;
  transition:all 0.2s;text-transform:uppercase}
.btn:hover{border-color:var(--accent);color:var(--accent);background:rgba(0,230,118,0.06)}
.btn.danger:hover{border-color:var(--red);color:var(--red);background:rgba(255,23,68,0.06)}
.pnl-pos{color:var(--accent)} .pnl-neg{color:var(--red)}
.league-badge{font-family:var(--mono);font-size:0.65rem;padding:2px 7px;border-radius:2px;
  background:rgba(0,230,118,0.08);color:var(--accent);border:1px solid var(--accent2)}
.toast{position:fixed;bottom:24px;right:24px;background:var(--bg2);border:1px solid var(--accent);
  color:var(--accent);font-family:var(--mono);font-size:0.8rem;padding:12px 20px;border-radius:4px;
  z-index:9999;transform:translateY(100px);opacity:0;transition:all 0.3s}
.toast.show{transform:translateY(0);opacity:1}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:14px">
    <div class="logo">⚽ SOCCER<span style="color:var(--text-dim)">/</span>AI</div>
    <div class="mode-badge">PAPER + REAL</div>
  </div>
  <div class="header-right">
    <div class="last-update" id="lastUpdate">Loading...</div>
    <div class="status-dot" id="statusDot"></div>
    <div class="clock" id="clock">--:--:--</div>
  </div>
</div>
<div class="nav">
  <div class="nav-tab active" onclick="switchTab('dashboard',this)">Dashboard</div>
  <div class="nav-tab" onclick="switchTab('tips',this)">Today's Tips</div>
  <div class="nav-tab" onclick="switchTab('bets',this)">Active Bets</div>
  <div class="nav-tab" onclick="switchTab('history',this)">History</div>
  <div class="nav-tab" onclick="switchTab('stats',this)">Statistics</div>
  <div class="nav-tab" onclick="switchTab('settings',this)">Settings</div>
</div>

<!-- DASHBOARD -->
<div id="page-dashboard" class="page active">
  <div class="grid grid-4" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Paper Bankroll</div>
      <div class="kpi-value" id="paperBankroll">$--</div>
      <div class="kpi-change" id="paperPnl">--</div>
      <div class="kpi-sub">Started at $500.00</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Real Bankroll</div>
      <div class="kpi-value" id="realBankroll">$--</div>
      <div class="kpi-change" id="realPnl">--</div>
      <div class="kpi-sub">Set with /setreal command</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Win Rate</div>
      <div class="kpi-value" id="winRate">--%</div>
      <div class="kpi-change" id="winsLosses">-- W / -- L</div>
      <div class="kpi-sub">Paper ROI: <span id="paperRoi">0.0</span>%</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Today's Tips</div>
      <div class="kpi-value" id="tipsCount">--</div>
      <div class="kpi-change" id="tipsLeagues">-- leagues</div>
      <div class="kpi-sub">Last scan: <span id="lastScan">--</span></div>
    </div>
  </div>
  <div class="grid grid-2-1" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>P&L Chart (Paper)</div>
      <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Today's Top Tips</div>
      <div class="scroll-panel" id="topTips" style="max-height:300px"></div>
    </div>
  </div>
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Pending Paper Bets</div>
      <div class="scroll-panel">
        <table class="data-table">
          <thead><tr><th>Match</th><th>Market</th><th>Selection</th><th>Odds</th><th>Stake</th><th>Potential</th></tr></thead>
          <tbody id="pendingBetsTbody"><tr><td colspan="6" class="empty">No pending bets</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Activity Log</div>
      <div class="scroll-panel" id="activityLog" style="max-height:300px"></div>
    </div>
  </div>
</div>

<!-- TIPS -->
<div id="page-tips" class="page">
  <div class="card" style="margin-bottom:16px">
    <div class="card-title"><span class="dot"></span>Today's Betting Tips
      <button class="btn" onclick="triggerAnalysis()" style="margin-left:auto">🔍 Run Analysis Now</button>
    </div>
    <div id="tipsList"></div>
  </div>
</div>

<!-- ACTIVE BETS -->
<div id="page-bets" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Paper Bets — Pending</div>
      <div class="scroll-panel" style="max-height:500px">
        <table class="data-table">
          <thead><tr><th>ID</th><th>Match</th><th>Market</th><th>Selection</th><th>Odds</th><th>Stake</th><th>To Win</th><th>Conf</th></tr></thead>
          <tbody id="paperBetsTbody"><tr><td colspan="8" class="empty">No paper bets</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Real Bets — Pending</div>
      <div class="scroll-panel" style="max-height:500px">
        <table class="data-table">
          <thead><tr><th>ID</th><th>Match</th><th>Selection</th><th>Odds</th><th>Stake</th><th>Conf</th></tr></thead>
          <tbody id="realBetsTbody"><tr><td colspan="6" class="empty">No real bets — set bankroll with /setreal</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- HISTORY -->
<div id="page-history" class="page">
  <div class="card">
    <div class="card-title"><span class="dot"></span>Settled Bet History</div>
    <div class="scroll-panel" style="max-height:600px">
      <table class="data-table">
        <thead><tr><th>ID</th><th>Match</th><th>Market</th><th>Selection</th><th>Odds</th><th>Stake</th><th>Result</th><th>Status</th><th>Profit</th></tr></thead>
        <tbody id="historyTbody"><tr><td colspan="9" class="empty">No settled bets yet</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- STATS -->
<div id="page-stats" class="page">
  <div class="grid grid-3" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Paper Performance</div>
      <div id="paperStats" style="font-family:var(--mono);font-size:0.82rem;line-height:2.2"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Real Performance</div>
      <div id="realStats" style="font-family:var(--mono);font-size:0.82rem;line-height:2.2"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bet Type Breakdown</div>
      <div class="chart-wrap" style="height:200px"><canvas id="betTypeChart"></canvas></div>
    </div>
  </div>
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Win/Loss by League</div>
      <div class="scroll-panel" id="leagueStats" style="max-height:300px"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Confidence vs Win Rate</div>
      <div class="chart-wrap"><canvas id="confChart"></canvas></div>
    </div>
  </div>
</div>

<!-- SETTINGS -->
<div id="page-settings" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Controls</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <button class="btn" onclick="botControl('resume')" style="color:var(--accent);border-color:var(--accent)">▶ Resume</button>
        <button class="btn danger" onclick="botControl('pause')">⏸ Pause</button>
        <button class="btn" onclick="botControl('settle')" style="grid-column:span 2">🔄 Settle Pending Bets</button>
        <button class="btn" onclick="fetchAll()" style="grid-column:span 2">↻ Refresh Dashboard</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Real Bankroll</div>
      <div style="font-family:var(--mono);font-size:0.8rem;color:var(--text-dim);margin-bottom:10px">
        Set your real money bankroll to enable real bet tracking
      </div>
      <input type="number" id="realBankrollInput" placeholder="e.g. 1000"
        style="background:var(--bg0);border:1px solid var(--border);color:var(--text);
        font-family:var(--mono);font-size:0.85rem;padding:8px 12px;border-radius:3px;width:100%;margin-bottom:10px"/>
      <button class="btn" onclick="setRealBankroll()" style="width:100%">💾 Save Bankroll</button>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Info</div>
      <div id="botInfo" style="font-family:var(--mono);font-size:0.8rem;line-height:2.2"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Covered Leagues</div>
      <div id="leagueList" style="display:flex;flex-wrap:wrap;gap:6px;padding-top:4px"></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let pnlChartObj=null, betTypeChartObj=null, confChartObj=null;

function updateClock(){document.getElementById('clock').textContent=new Date().toUTCString().slice(17,25)+' UTC'}
setInterval(updateClock,1000); updateClock();

function switchTab(name,el){
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('page-'+name).classList.add('active');
  if(name==='stats') renderStats(window._lastData||{});
}

function showToast(msg,type='info'){
  const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show');
  t.style.borderColor=type==='error'?'var(--red)':type==='success'?'var(--accent)':'var(--blue)';
  t.style.color=type==='error'?'var(--red)':type==='success'?'var(--accent)':'var(--blue)';
  setTimeout(()=>t.classList.remove('show'),3500);
}

function fmt(n,d=2){return n==null?'--':Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d})}
function sign(n){return n>0?'+':''}
function pnlClass(n){return n>0?'pnl-pos':n<0?'pnl-neg':'neutral'}
function statusBadge(s){return `<span class="badge ${s}">${s.toUpperCase()}</span>`}
function riskColor(r){return {low:'var(--accent)',medium:'var(--yellow)',high:'var(--red)'}[r]||'var(--text-dim)'}

async function fetchAll(){
  try{
    const r=await fetch('/api/state');
    const d=await r.json();
    window._lastData=d;
    renderAll(d);
    document.getElementById('lastUpdate').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('lastUpdate').textContent='Update failed'}
}

function renderAll(d){
  // KPIs
  const pp=d.stats?.paper||{}, rp=d.stats?.real||{};
  document.getElementById('paperBankroll').textContent='$'+fmt(d.paper_bankroll||500);
  const ppnl=pp.profit||0;
  const ppnlEl=document.getElementById('paperPnl');
  ppnlEl.textContent=(ppnl>=0?'+$':'-$')+fmt(Math.abs(ppnl))+' P&L';
  ppnlEl.className='kpi-change '+(ppnl>=0?'up':'down');
  document.getElementById('realBankroll').textContent='$'+fmt(d.real_bankroll||0);
  const rpnl=rp.profit||0;
  const rpnlEl=document.getElementById('realPnl');
  rpnlEl.textContent=(rpnl>=0?'+$':'-$')+fmt(Math.abs(rpnl))+' P&L';
  rpnlEl.className='kpi-change '+(rpnl>=0?'up':'down');
  const total=pp.won+pp.lost||0;
  const wr=total>0?(pp.won/total*100):0;
  document.getElementById('winRate').textContent=fmt(wr,1)+'%';
  document.getElementById('winsLosses').textContent=(pp.won||0)+' W / '+(pp.lost||0)+' L';
  document.getElementById('paperRoi').textContent=fmt(pp.roi||0,1);
  const tips=d.tips_today||[];
  document.getElementById('tipsCount').textContent=tips.length;
  document.getElementById('tipsLeagues').textContent=[...new Set(tips.map(t=>t.league))].length+' leagues';
  document.getElementById('lastScan').textContent=d.last_scan?(d.last_scan.slice(11,16)+' UTC'):'Never';

  // PnL chart from history
  const hist=d.bet_history||[];
  const settled=hist.filter(b=>b.type==='paper'&&b.profit!=null).slice(0,30).reverse();
  let running=500,labels=[],vals=[];
  settled.forEach(b=>{running+=b.profit;labels.push(b.id);vals.push(round2(running));});
  if(!pnlChartObj){
    const ctx=document.getElementById('pnlChart').getContext('2d');
    pnlChartObj=new Chart(ctx,{type:'line',data:{labels,datasets:[
      {label:'Bankroll',data:vals,borderColor:'#00e676',backgroundColor:'rgba(0,230,118,0.08)',borderWidth:2,pointRadius:2,fill:true,tension:0.3},
      {label:'Start',data:labels.map(()=>500),borderColor:'rgba(90,154,90,0.4)',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{display:false},y:{grid:{color:'rgba(42,74,42,0.5)'},ticks:{color:'#5a8a5a',font:{family:'IBM Plex Mono',size:10},callback:v=>'$'+v}}}}});
  }else{pnlChartObj.data.labels=labels;pnlChartObj.data.datasets[0].data=vals;pnlChartObj.data.datasets[1].data=labels.map(()=>500);pnlChartObj.update('none')}

  // Top tips
  const sorted=[...tips].sort((a,b)=>b.confidence-a.confidence).slice(0,5);
  document.getElementById('topTips').innerHTML=sorted.length?sorted.map(t=>`
    <div style="padding:10px 0;border-bottom:1px solid var(--border)">
      <div style="font-family:var(--mono);font-weight:700;font-size:0.85rem;color:var(--text-bright)">${t.match}</div>
      <div style="display:flex;gap:8px;margin:4px 0;flex-wrap:wrap">
        <span class="league-badge">${t.league}</span>
        <span style="font-family:var(--mono);font-size:0.7rem;color:var(--text-dim)">${t.market}</span>
        <span style="font-family:var(--mono);font-size:0.72rem;font-weight:600;color:var(--accent)">${t.selection} @ ${t.odds}</span>
        <span style="font-family:var(--mono);font-size:0.68rem;color:${riskColor(t.risk)}">${t.confidence}%</span>
      </div>
      <div style="font-size:0.75rem;color:var(--text-dim)">${(t.reasoning||'').slice(0,80)}${(t.reasoning||'').length>80?'…':''}</div>
    </div>`).join(''):'<div style="color:var(--text-dim);padding:20px;text-align:center;font-family:var(--mono);font-size:0.8rem">No tips yet — click Run Analysis</div>';

  // Pending bets table
  const pending=(d.paper_bets||[]).filter(b=>b.status==='pending');
  document.getElementById('pendingBetsTbody').innerHTML=pending.length?pending.slice(0,10).map(b=>`<tr>
    <td style="color:var(--accent)">${b.match.split(' vs ')[0]} vs<br>${b.match.split(' vs ')[1]||''}</td>
    <td style="font-size:0.72rem;color:var(--text-dim)">${b.market}</td>
    <td><b>${b.selection}</b></td>
    <td style="color:var(--yellow)">${b.odds}</td>
    <td>$${fmt(b.stake)}</td>
    <td class="up">$${fmt(b.potential_win)}</td>
  </tr>`).join(''):'<tr><td colspan="6" class="empty">No pending bets</td></tr>';

  // Activity log
  document.getElementById('activityLog').innerHTML=(d.activity_log||[]).slice(0,15).map(a=>`
    <div style="padding:7px 0;border-bottom:1px solid rgba(42,74,42,0.4);font-family:var(--mono);font-size:0.75rem">
      <span style="color:var(--text-dim)">${(a.time||'').slice(11,16)}</span>
      <span style="margin-left:8px;color:var(--text)">${a.event}</span>
    </div>`).join('')||'<div style="color:var(--text-dim);padding:20px;text-align:center">No activity yet</div>';

  // Tips page
  document.getElementById('tipsList').innerHTML=tips.length?tips.map(t=>`
    <div class="tip-card ${t.risk}">
      <div class="tip-match">⚽ ${t.match}</div>
      <div class="tip-meta">
        <span class="league-badge">${t.league}</span>
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)">${(t.kickoff||'').slice(11,16)} UTC</span>
        <span style="font-family:var(--mono);font-size:0.72rem;font-weight:600;color:var(--yellow)">${t.market}</span>
        <span style="font-family:var(--mono);font-size:0.8rem;font-weight:700;color:var(--text-bright)">${t.selection}</span>
        <span style="font-family:var(--mono);font-size:0.75rem;color:var(--accent)">@ ${t.odds}</span>
        <span style="font-family:var(--mono);font-size:0.7rem;color:${riskColor(t.risk)}">${t.confidence}% conf</span>
        <span style="font-family:var(--mono);font-size:0.7rem;color:var(--text-dim)">Stake: $${fmt(t.paper_stake||0)}</span>
      </div>
      <div class="tip-reasoning">${t.reasoning||''}</div>
      ${t.predicted_score?`<div style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);margin-top:4px">Predicted score: ${t.predicted_score}</div>`:''}
    </div>`).join(''):'<div style="color:var(--text-dim);padding:40px;text-align:center;font-family:var(--mono)">No tips generated yet for today.<br><br><button class="btn" onclick="triggerAnalysis()">🔍 Run Analysis Now</button></div>';

  // Active bets
  document.getElementById('paperBetsTbody').innerHTML=pending.length?pending.map(b=>`<tr>
    <td style="color:var(--text-dim);font-size:0.72rem">${b.id}</td>
    <td style="font-size:0.75rem">${b.match}</td>
    <td style="font-size:0.72rem;color:var(--text-dim)">${b.market}</td>
    <td><b>${b.selection}</b></td>
    <td style="color:var(--yellow)">${b.odds}</td>
    <td>$${fmt(b.stake)}</td>
    <td class="up">$${fmt(b.potential_win)}</td>
    <td style="color:${b.confidence>=70?'var(--accent)':'var(--text-dim)'}">${b.confidence}%</td>
  </tr>`).join(''):'<tr><td colspan="8" class="empty">No pending paper bets</td></tr>';

  const realPending=(d.real_bets||[]).filter(b=>b.status==='pending');
  document.getElementById('realBetsTbody').innerHTML=realPending.length?realPending.map(b=>`<tr>
    <td style="color:var(--text-dim)">${b.id}</td>
    <td style="font-size:0.75rem">${b.match}</td>
    <td><b>${b.selection}</b></td>
    <td style="color:var(--yellow)">${b.odds}</td>
    <td>$${fmt(b.stake)}</td>
    <td>${b.confidence}%</td>
  </tr>`).join(''):'<tr><td colspan="6" class="empty">Set real bankroll with /setreal command</td></tr>';

  // History
  const hist2=d.bet_history||[];
  document.getElementById('historyTbody').innerHTML=hist2.length?hist2.slice(0,50).map(b=>`<tr>
    <td style="color:var(--text-dim);font-size:0.7rem">${b.id}</td>
    <td style="font-size:0.75rem">${b.match}</td>
    <td style="font-size:0.72rem;color:var(--text-dim)">${b.market}</td>
    <td><b>${b.selection}</b></td>
    <td style="color:var(--yellow)">${b.odds}</td>
    <td>$${fmt(b.stake)}</td>
    <td style="font-size:0.72rem;color:var(--text-dim)">${b.result||'--'}</td>
    <td>${statusBadge(b.status)}</td>
    <td class="${pnlClass(b.profit)}">${b.profit!=null?(sign(b.profit)+'$'+fmt(Math.abs(b.profit))):'--'}</td>
  </tr>`).join(''):'<tr><td colspan="9" class="empty">No settled bets yet</td></tr>';

  // Bot info
  document.getElementById('botInfo').innerHTML=`
    Running since: <b style="color:var(--accent)">${(d.start_time||'').slice(0,10)}</b><br>
    Status: <b style="${d.paused?'color:var(--red)':'color:var(--accent)'}">${d.paused?'PAUSED':'RUNNING'}</b><br>
    Today's tips: <b style="color:var(--accent)">${tips.length}</b><br>
    Pending bets: <b style="color:var(--accent)">${pending.length}</b><br>
    Total settled: <b style="color:var(--accent)">${hist2.length}</b>`;

  // Leagues
  document.getElementById('leagueList').innerHTML=(d.leagues||[]).map(l=>`<span class="league-badge">${l}</span>`).join('');

  // Settings
  if(d.real_bankroll) document.getElementById('realBankrollInput').placeholder=`Current: $${fmt(d.real_bankroll)}`;
}

function renderStats(d){
  const pp=d.stats?.paper||{}, rp=d.stats?.real||{};
  const totalP=pp.won+pp.lost||0, totalR=rp.won+rp.lost||0;
  document.getElementById('paperStats').innerHTML=`
    Bankroll: <b style="color:var(--accent)">$${fmt(d.paper_bankroll||500)}</b><br>
    ROI: <b style="color:${(pp.roi||0)>=0?'var(--accent)':'var(--red)'}">${fmt(pp.roi||0,1)}%</b><br>
    Won: <b style="color:var(--accent)">${pp.won||0}</b> / Lost: <b style="color:var(--red)">${pp.lost||0}</b><br>
    Win rate: <b>${totalP>0?fmt(pp.won/totalP*100,1):0}%</b><br>
    Total profit: <b class="${pnlClass(pp.profit||0)}">${sign(pp.profit||0)}$${fmt(Math.abs(pp.profit||0))}</b>`;
  document.getElementById('realStats').innerHTML=`
    Bankroll: <b style="color:var(--accent)">$${fmt(d.real_bankroll||0)}</b><br>
    ROI: <b style="color:${(rp.roi||0)>=0?'var(--accent)':'var(--red)'}">${fmt(rp.roi||0,1)}%</b><br>
    Won: <b style="color:var(--accent)">${rp.won||0}</b> / Lost: <b style="color:var(--red)">${rp.lost||0}</b><br>
    Win rate: <b>${totalR>0?fmt(rp.won/totalR*100,1):0}%</b><br>
    Total profit: <b class="${pnlClass(rp.profit||0)}">${sign(rp.profit||0)}$${fmt(Math.abs(rp.profit||0))}</b>`;

  // Bet type breakdown
  const hist=d.bet_history||[];
  const byType={};
  hist.forEach(b=>{
    const t=b.market||'Other';
    if(!byType[t]) byType[t]={won:0,total:0};
    byType[t].total++;
    if(b.status==='won') byType[t].won++;
  });
  const btLabels=Object.keys(byType).slice(0,6);
  const btData=btLabels.map(k=>byType[k].total);
  if(!betTypeChartObj){
    const ctx=document.getElementById('betTypeChart').getContext('2d');
    betTypeChartObj=new Chart(ctx,{type:'doughnut',data:{labels:btLabels,datasets:[{data:btData,
      backgroundColor:['rgba(0,230,118,0.7)','rgba(0,168,255,0.7)','rgba(255,214,0,0.7)','rgba(255,23,68,0.7)','rgba(90,154,90,0.5)','rgba(200,232,200,0.4)'],borderWidth:0}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'60%',plugins:{legend:{labels:{color:'#5a8a5a',font:{family:'IBM Plex Mono',size:10}}}}}});
  }else{betTypeChartObj.data.labels=btLabels;betTypeChartObj.data.datasets[0].data=btData;betTypeChartObj.update()}

  // League breakdown
  const byLeague={};
  hist.forEach(b=>{const l=b.league||'Other';if(!byLeague[l]){byLeague[l]={won:0,lost:0,profit:0}};if(b.status==='won')byLeague[l].won++;else if(b.status==='lost')byLeague[l].lost++;byLeague[l].profit+=(b.profit||0)});
  document.getElementById('leagueStats').innerHTML=Object.entries(byLeague).map(([l,s])=>`
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)">
      <span class="league-badge">${l}</span>
      <span style="font-family:var(--mono);font-size:0.75rem;color:var(--text-dim)">${s.won}W/${s.lost}L</span>
      <span style="font-family:var(--mono);font-size:0.78rem" class="${pnlClass(s.profit)}">${sign(s.profit)}$${fmt(Math.abs(s.profit))}</span>
    </div>`).join('')||'<div style="color:var(--text-dim);padding:20px;text-align:center">No data yet</div>';
}

function round2(n){return Math.round(n*100)/100}

async function botControl(action){
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    const d=await r.json(); showToast(d.message||'Done','success'); fetchAll();
  }catch(e){showToast('Error','error')}
}

async function triggerAnalysis(){
  showToast('Analysis running — check back in a few minutes','info');
  await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'analyze'})});
}

async function setRealBankroll(){
  const val=parseFloat(document.getElementById('realBankrollInput').value);
  if(!val||val<=0){showToast('Enter a valid amount','error');return}
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'set_real_bankroll',value:val})});
    const d=await r.json(); showToast(d.message,'success'); fetchAll();
  }catch(e){showToast('Error','error')}
}

fetchAll();
setInterval(fetchAll,30000);
</script>
</body>
</html>"""


@flask_app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@flask_app.route("/api/state")
def api_state():
    return jsonify({
        **state,
        "leagues": list(LEAGUE_IDS.keys()),
    })


@flask_app.route("/api/control", methods=["POST"])
def api_control():
    data   = request.get_json()
    action = data.get("action")
    if action == "pause":
        state["paused"] = True
        return jsonify({"message": "Bot paused"})
    elif action == "resume":
        state["paused"] = False
        return jsonify({"message": "Bot resumed"})
    elif action == "settle":
        settle_bets()
        return jsonify({"message": "Bets settled"})
    elif action == "set_real_bankroll":
        val = float(data.get("value", 0))
        state["real_bankroll"] = val
        save_state()
        return jsonify({"message": f"Real bankroll set to ${val:.2f}"})
    elif action == "analyze":
        def _run():
            if _bot_loop:
                import asyncio as _a
                future = _a.run_coroutine_threadsafe(run_daily_tips(), _bot_loop)
                try:
                    future.result(timeout=300)
                except Exception as e:
                    logger.error(f"Analysis error: {e}")
        import threading as _t
        _t.Thread(target=_run, daemon=True).start()
        return jsonify({"message": "Analysis triggered — tips will arrive on Telegram shortly"})
    return jsonify({"message": "Unknown action"}), 400


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM BOT RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def run_telegram_bot():
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        global telegram_bot
        request_obj = HTTPXRequest(connect_timeout=30, read_timeout=30,
                                   write_timeout=30, pool_timeout=30)

        # Delete webhook first — prevent 409
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
                    json={"drop_pending_updates": True}, timeout=10,
                )
        except:
            pass

        logger.info("Waiting 8s for previous instance to die...")
        await asyncio.sleep(8)

        app_tg = Application.builder().token(TELEGRAM_TOKEN).request(request_obj).build()
        telegram_bot = app_tg.bot
        _bot_loop    = asyncio.get_event_loop()

        app_tg.add_handler(CommandHandler("start",    cmd_start))
        app_tg.add_handler(CommandHandler("tips",     cmd_tips))
        app_tg.add_handler(CommandHandler("stats",    cmd_stats))
        app_tg.add_handler(CommandHandler("bets",     cmd_bets))
        app_tg.add_handler(CommandHandler("bankroll", cmd_bankroll))
        app_tg.add_handler(CommandHandler("setreal",  cmd_setreal))
        app_tg.add_handler(CommandHandler("settle",   cmd_settle))
        app_tg.add_handler(CommandHandler("pause",    cmd_pause))
        app_tg.add_handler(CommandHandler("resume",   cmd_resume))
        app_tg.add_handler(CommandHandler("analyze",  cmd_analyze))

        for attempt in range(1, 6):
            try:
                await app_tg.initialize()
                break
            except Exception as e:
                logger.warning(f"Telegram init attempt {attempt} failed: {e}")
                await asyncio.sleep(attempt * 5)

        await app_tg.start()
        await app_tg.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info("Soccer bot Telegram polling started ✅")
        await scheduler(app_tg)

    loop.run_until_complete(run())


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()
    logger.info("Soccer betting bot thread started")

    port = int(os.environ.get("PORT", 5001))
    logger.info(f"Dashboard starting on port {port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
