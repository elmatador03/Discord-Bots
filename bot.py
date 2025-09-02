import discord
from discord import app_commands
from discord.ui import Button, View, Modal, TextInput
import aiohttp
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
import sqlite3
import json

# Constants
TOKEN = ''  # Replace
PREDICTION_CHANNEL_ID =   # Replace
RESULTS_CHANNEL_ID =  # Replace
TZ = pytz.timezone('America/New_York')
TICKERS = ['BTC', 'ETH', 'XRP', 'SOL', 'HYPE']
COINGECKO_IDS = 'bitcoin,ethereum,ripple,solana,hyperliquid'
DB_FILE = 'predictions.db'

# Setup Database
conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS predictions (
        week_start TEXT,
        user_id INTEGER,
        ticker TEXT,
        predicted REAL,
        actual REAL,
        accuracy REAL,
        PRIMARY KEY (week_start, user_id, ticker)
    )
''')
cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_stats (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        total_predictions INTEGER DEFAULT 0,
        average_accuracy REAL DEFAULT 0,
        quarterly_accuracies TEXT DEFAULT '{}',  -- JSON {quarter: {"avg": x, "count": n}}
        yearly_accuracies TEXT DEFAULT '{}'      -- JSON {year: {"avg": x, "count": n}}
    )
''')
conn.commit()

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
scheduler = AsyncIOScheduler(timezone=TZ)

# Global flags
prediction_window_open = False

async def fetch_prices():
    async with aiohttp.ClientSession() as session:
        url = f'https://api.coingecko.com/api/v3/simple/price?ids={COINGECKO_IDS}&vs_currencies=usd'
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                ids_list = COINGECKO_IDS.split(',')
                prices = {TICKERS[i]: data.get(ids_list[i], {}).get('usd', 0) for i in range(len(TICKERS))}
                return prices
            else:
                print(f"API error: {resp.status}")
                return {ticker: 0 for ticker in TICKERS}

def get_week_start():
    now = datetime.now(TZ)
    week_start = now - timedelta(days=now.weekday())  # Monday start
    return week_start.strftime('%Y-%m-%d')

def calculate_accuracy(predicted, actual):
    if actual == 0:
        return 0
    return 100 - (abs(predicted - actual) / actual * 100)

class PredictionModal(Modal):
    def __init__(self):
        super().__init__(title="Submit Predictions")
        for ticker in TICKERS:
            self.add_item(TextInput(label=f"{ticker} Price Prediction", placeholder="Enter USD price", required=True))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            preds = {}
            for i, child in enumerate(self.children):
                ticker = TICKERS[i]
                preds[ticker] = float(child.value)
            
            week_start = get_week_start()
            user_id = interaction.user.id
            
            # Double-check in case, but should be checked before showing modal
            cursor.execute("SELECT 1 FROM predictions WHERE week_start=? AND user_id=?", (week_start, user_id))
            if cursor.fetchone():
                await interaction.response.send_message("You've already submitted predictions this week.", ephemeral=True)
                return
            
            for ticker, predicted in preds.items():
                cursor.execute("INSERT OR REPLACE INTO predictions (week_start, user_id, ticker, predicted) VALUES (?, ?, ?, ?)", 
                               (week_start, user_id, ticker, predicted))
            conn.commit()
            
            cursor.execute("INSERT OR IGNORE INTO user_stats (user_id, username) VALUES (?, ?)", (user_id, interaction.user.name))
            cursor.execute("UPDATE user_stats SET username=? WHERE user_id=?", (interaction.user.name, user_id))
            conn.commit()
            
            await interaction.response.send_message("Predictions saved!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid input: Please enter numbers only.", ephemeral=True)

async def open_prediction_window():
    global prediction_window_open
    prediction_window_open = True
    channel = bot.get_channel(PREDICTION_CHANNEL_ID)
    if channel:
        view = View(timeout=None)  # Persistent until bot restarts or edited
        button = Button(label="Submit Predictions", style=discord.ButtonStyle.primary, emoji="🔮")
        
        async def button_callback(interaction: discord.Interaction):
            if not prediction_window_open:
                await interaction.response.send_message("Prediction window is closed.", ephemeral=True)
                return
            
            week_start = get_week_start()
            user_id = interaction.user.id
            cursor.execute("SELECT 1 FROM predictions WHERE week_start=? AND user_id=?", (week_start, user_id))
            if cursor.fetchone():
                await interaction.response.send_message("You've already submitted predictions this week.", ephemeral=True)
                return
            
            modal = PredictionModal()
            await interaction.response.send_modal(modal)
        
        button.callback = button_callback
        view.add_item(button)
        
        await channel.send("🔮 Prediction window is now open! Click the button to submit your predictions (closes Wednesday 11:59 PM ET).", view=view)

async def close_prediction_window():
    global prediction_window_open
    prediction_window_open = False
    channel = bot.get_channel(PREDICTION_CHANNEL_ID)
    if channel:
        await channel.send("Prediction window is closed. Results Sunday at 8:01 PM ET!")

async def process_results():
    prices = await fetch_prices()
    week_start = get_week_start()
    
    cursor.execute("SELECT user_id, ticker, predicted FROM predictions WHERE week_start=? AND actual IS NULL", (week_start,))
    preds = cursor.fetchall()
    
    if not preds:
        channel = bot.get_channel(RESULTS_CHANNEL_ID)
        if channel:
            await channel.send("No predictions this week.")
        return
    
    user_data = {}
    user_accuracies = {}
    for user_id, ticker, predicted in preds:
        actual = prices.get(ticker, 0)
        accuracy = calculate_accuracy(predicted, actual)
        cursor.execute("UPDATE predictions SET actual=?, accuracy=? WHERE week_start=? AND user_id=? AND ticker=?", 
                       (actual, accuracy, week_start, user_id, ticker))
        user_data.setdefault(user_id, {})[ticker] = (predicted, actual, accuracy)
        user_accuracies.setdefault(user_id, []).append(accuracy)
    
    conn.commit()
    
    # Update user stats
    now = datetime.now(TZ)
    quarter = f"{now.year}-Q{((now.month-1)//3 + 1)}"
    year = str(now.year)
    
    for user_id, accs in user_accuracies.items():
        avg_acc = sum(accs) / len(accs)
        cursor.execute("INSERT OR IGNORE INTO user_stats (user_id, username) VALUES (?, 'placeholder')", (user_id,))
        cursor.execute("""
            UPDATE user_stats SET 
                total_predictions = total_predictions + 1,
                average_accuracy = (average_accuracy * total_predictions + ?) / (total_predictions + 1)
            WHERE user_id=?
        """, (avg_acc, user_id))
        
        cursor.execute("SELECT quarterly_accuracies, yearly_accuracies FROM user_stats WHERE user_id=?", (user_id,))
        q_json, y_json = cursor.fetchone()
        q_dict = json.loads(q_json)
        y_dict = json.loads(y_json)
        
        # Update quarterly
        q_data = q_dict.get(quarter, {"avg": 0, "count": 0})
        new_q_avg = (q_data["avg"] * q_data["count"] + avg_acc) / (q_data["count"] + 1)
        q_data["avg"] = new_q_avg
        q_data["count"] += 1
        q_dict[quarter] = q_data
        
        # Update yearly
        y_data = y_dict.get(year, {"avg": 0, "count": 0})
        new_y_avg = (y_data["avg"] * y_data["count"] + avg_acc) / (y_data["count"] + 1)
        y_data["avg"] = new_y_avg
        y_data["count"] += 1
        y_dict[year] = y_data
        
        cursor.execute("UPDATE user_stats SET quarterly_accuracies=?, yearly_accuracies=? WHERE user_id=?", 
                       (json.dumps(q_dict), json.dumps(y_dict), user_id))
    
    conn.commit()
    
    # Post results
    embed = discord.Embed(title="Weekly Prediction Results", color=0x00ff00)
    actual_prices_str = "\n".join(f"{ticker}: ${price:.2f}" for ticker, price in prices.items())
    embed.add_field(name="Actual Prices", value=actual_prices_str, inline=False)
    
    sorted_users = sorted(user_data.items(), key=lambda x: sum(d[2] for d in x[1].values()) / len(x[1]), reverse=True)[:10]
    for rank, (user_id, data) in enumerate(sorted_users, 1):
        user = await bot.fetch_user(user_id)
        avg_acc = sum(d[2] for d in data.values()) / len(data)
        field_value = f"Avg Accuracy: {avg_acc:.2f}%\n" + "\n".join(f"{ticker}: Pred ${d[0]:.2f} / Actual ${d[1]:.2f} (Acc: {d[2]:.2f}%)" for ticker, d in sorted(data.items()))
        embed.add_field(name=f"#{rank} {user.name}", value=field_value, inline=False)
    
    channel = bot.get_channel(RESULTS_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    await tree.sync(guild=discord.Object(id=1410246620397703274))
    print("Slash commands synced to guild!") # Replace YOUR_GUILD_ID with the copied number
    scheduler.add_job(open_prediction_window, CronTrigger(day_of_week='wed', hour=13, minute=53, timezone=TZ))
    scheduler.add_job(close_prediction_window, CronTrigger(day_of_week='wed', hour=13, minute=54, timezone=TZ))
    scheduler.add_job(process_results, CronTrigger(day_of_week='wed', hour=13, minute=55, timezone=TZ))
    scheduler.start()

@tree.command(name="open_window", description="Manually open the prediction window")
async def open_window(interaction: discord.Interaction):
    await open_prediction_window()
    await interaction.response.send_message("Window opened manually.", ephemeral=True)

@tree.command(name="process_results_now", description="Manually process results")
async def process_results_now(interaction: discord.Interaction):
    await process_results()
    await interaction.response.send_message("Results processed manually.", ephemeral=True)


bot.run(TOKEN)
