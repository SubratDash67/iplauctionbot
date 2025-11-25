# Discord IPL Auction Bot - Setup Guide

## Step 1: Create Discord Bot

1. Go to https://discord.com/developers/applications
2. Click "New Application" > Name it > Create
3. Go to "Bot" in left menu > "Add Bot"
4. Click "Reset Token" > Copy and save the token
5. Enable these under "Privileged Gateway Intents":
   - MESSAGE CONTENT INTENT
   - SERVER MEMBERS INTENT
6. Go to "OAuth2" > "URL Generator"
7. Check: `bot` and `applications.commands`
8. Check permissions: `Administrator`
9. Copy the generated URL > Open in browser > Select your server > Authorize

## Step 2: Install Requirements

```bash
pip install -r requirements.txt
```

## Step 3: Add Bot Token

Create `.env` file in the bot folder:
```
DISCORD_TOKEN=your_token_here
```

## Step 4: Run the Bot

```
python Bot.py
```

Wait for:
```
Syncing slash commands globally...
Slash commands synced!
Logged in as BotName (ID: xxx)
Bot is ready! Use /help to see all commands.
```

Slash commands take 1-2 minutes to appear in Discord after first run.

---

## Commands

### Team Assignment (Admin)
| Command | Description |
|---------|-------------|
| `/assignteam @user TEAM` | Assign user to team |
| `/unassignteam @user` | Remove assignment |
| `/showteams` | Show all assignments |
| `/myteam` | Check your team |

### Bidding
| Command | Description |
|---------|-----------|
| `/bid` | Place bid (auto-detects your team) |
| `/bidhistory [limit]` | Show recent bid history |
| `/teamsquad TEAM` | Show players bought by a team |

### List Management
| Command | Description |
|---------|-------------|
| `/createlist name` | Create player list |
| `/addplayer list player` | Add player to list |
| `/loadcsv list filepath` | Load CSV file |
| `/showlists` | Show all lists |
| `/setorder list1 list2` | Set auction order |

### Auction Control (Admin)
| Command | Description |
|---------|-----------|
| `/start` | Start auction |
| `/stop` | Stop auction |
| `/pause` | Pause auction |
| `/resume` | Resume auction |
| `/soldto TEAM` | Manually finalize sale (15s cooldown) |
| `/unsold` | Mark player unsold |
| `/clear` | Reset everything |

### Admin Actions
| Command | Description |
|---------|-----------|
| `/rollback` | Undo last player sale |
| `/release TEAM PLAYER` | Release retained player to auction |

### Settings (Admin)
| Command | Description |
|---------|-----------|
| `/setpurse TEAM amount` | Set team purse |

### Info
| Command | Description |
|---------|-------------|
| `/showpurse` | Show team purses |
| `/status` | Show auction status |
| `/help` | Show commands |

---

## Quick Start

**On First Run:**
- Bot automatically loads IPL 2025 player list from CSV
- Retained players added to teams (purses auto-deducted)
- Ready to auction immediately

**Teams:** MI, CSK, RCB, KKR, SRH, RR, DC, PBKS, GT, LSG  
**Starting Purse:** 120 Crore (after retained player deductions)

---

## How to Run Auction

**Setup (One-time):**
```
/assignteam @user TEAM    # Assign each user to a team
/showpurse                # Verify purses (adjusted for retained players)
```

**Optional - Release Retained Players:**
```
/release MI "Rohit Sharma"    # Release player back to auction
/teamsquad MI                 # View current squad
```

**Start Auction:**
```
/start    # Begin auction with auto-loaded player list
```

**During Auction:**
- Player announced → Users `/bid` to place bids
- **Timers:**
  - No bid in 60s → Player goes UNSOLD
  - After first bid → No bid in 120s → AUTO-SOLD to highest bidder
  - 20s gap between players
- **Admin Controls:**
  - `/soldto TEAM` - Manually sell (15s cooldown after last bid)
  - `/unsold` - Mark unsold and skip
  - `/rollback` - Undo last sale if mistake

**View Results:**
```
/teamsquad MI       # Players bought by MI
/bidhistory 20      # Recent 20 bids
/showpurse          # Remaining purses
```

**Data Saved:**
- `auction_data.xlsx` - Player | Team | Price | Time
- `auction.db` - Complete database with audit trail

---

## Troubleshooting

**Commands not showing:**
- Wait 1-2 minutes after first run
- Restart Discord client

**Bot not responding:**
- Check token in `.env` file
- Verify Message Content Intent is enabled
- Check console for errors

**Auction frozen after restart:**
- Bot auto-resets to inactive for safety
- Use `/start` to begin new auction

**Cannot use /soldto:**
- 15s cooldown after last bid (shown in error)
- Use `/unsold` to skip player without cooldown

