# Kalshi 15-Min Bot

Predicts UP or DOWN on Kalshi's 15-minute BTC, Gold, WTI Oil and Natural Gas markets and trades the setup.
Every trade is paper-traded first. **Live auto-trading unlocks per market only after that market
passes every approval gate**, and stays inside hard risk caps. Text alerts + black dashboard.

Not financial advice. Paper results can overstate real results.

## How it decides (each 15-minute window)
1. Reads the window: target price, time left, Kalshi's YES/NO prices.
2. Measures momentum over the last 5 minutes → **UP** or **DOWN**.
3. Prices the odds of that direction winning (distance to target, time left, volatility, momentum).
4. **Waits until 7:30 remains.** Between 7:30 and 2:00 it trades only if:
   - momentum is strong (≥ 0.5 std devs) and the last minute still agrees,
   - the odds beat Kalshi's price by ≥ 5¢ after fees and 1¢ slippage,
   - price is 5¢–90¢, enough contracts offered, underlying price is live (not delayed).
5. One trade per market per window. Never trades against momentum.

Dashboard card statuses: WAITING → SETUP FORMING → TRADE → IN TRADE, or CLOSED / BLOCKED.

## Approval gates (all must pass, per market)
| Gate | Default |
|---|---|
| Settled paper trades | ≥ 150 |
| Days tested | ≥ 7 |
| Net profit after fees | > $0 |
| Profit factor | ≥ 1.25 |
| Win rate beats market-implied odds | ≥ +3 points |
| Max drawdown (1-contract units) | ≤ $15 |
| Momentum improves predictions vs. no-momentum model | Brier score lower |
| Last 50 trades profitable | > $0 |

Gold, Oil and Natural Gas use free Yahoo prices, which are often delayed. The bot blocks trades on delayed prices,
so those markets may never unlock without a paid real-time feed.

Gates are re-checked every cycle. An asset that starts failing drops back to paper automatically.

## Live safety
- `TRADING_MODE=live` **and** passing gates **and** API key **and** kill switch off.
- Immediate-or-cancel orders only (never rests on the book).
- Caps: $10 / 5 contracts per trade, 3 open positions, 20 trades/day, $25 daily loss.
- Auto kill switch on daily loss limit or 6 live losses in a row. Big red STOP button on the dashboard.
- Test first with `KALSHI_DEMO=true` (Kalshi demo exchange, fake money; needs a demo API key).

## Deploy on DigitalOcean
1. Create a GitHub repo (private) and push this folder.
2. Create an Ubuntu 24.04 droplet ($6/mo, 1 GiB, New York region).
3. SSH in as root:
   ```bash
   git clone https://github.com/<you>/kalshi-scanner.git && cd kalshi-scanner
   bash deploy/setup_droplet.sh
   ```
   It prints your dashboard address (`https://<ip-with-dashes>.sslip.io`), username and a generated password.
   Free HTTPS is set up automatically with Caddy. Port 8080 is not exposed to the internet.
4. Edit `/opt/kalshi-scanner/.env` for text alerts, then `systemctl restart kalshi-engine kalshi-dashboard`.
5. Updates later: `bash ~/kalshi-scanner/deploy/update.sh` (keeps your settings, history and Kalshi key)
6. Logs: `journalctl -u kalshi-engine -f`

## Put it on your phone
**iPhone (Safari):** open the dashboard address → sign in → Share button → **Add to Home Screen**.
**Android (Chrome):** open the dashboard address → sign in → ⋮ menu → **Install app** (or Add to Home screen).

It opens full-screen like a normal app, stays signed in for 30 days, and refreshes every 5 seconds.
Text alerts reach you even when the app is closed.

## Text alerts
- **Free:** Gmail → carrier text gateway. Set `ALERT_METHOD=email_sms`, a Gmail App Password, and
  `SMS_GATEWAY_TO` (Verizon `@vtext.com`, T-Mobile `@tmomail.net` — e.g. `4145551234@tmomail.net`). AT&T no longer supports this.
- **Any carrier:** Twilio (~1¢/text). Set `ALERT_METHOD=twilio` and the four `TWILIO_*` values.

## Paper / Live switch
The **Trading mode** switch at the top of the dashboard (phone too) flips between PAPER and LIVE.
- LIVE needs a connected Kalshi account and typing `LIVE` to confirm.
- Even in LIVE, only markets that passed every approval gate trade real money; the rest stay on paper.
- Switching back to PAPER is instant. The red STOP button also halts live orders.

## Connect Kalshi (API key)
Dashboard → **Kalshi account → Connect**. Paste the Key ID and the full private key, tick "demo" if it's a
demo-account key, and press **Connect & test**. The bot makes one balance check to prove the key works, saves it
on your server only (`keys/kalshi_key.pem`, owner-read-only), and starts using it within seconds — no restart.
**Disconnect** deletes the key and returns to PAPER. Tip: connect a demo key first and run LIVE on fake money.

## Google Sheets
Dashboard → **Download CSV** → Google Sheets → File → Import.

## Tests
`pip install -r requirements.txt pytest && python -m pytest -q`
