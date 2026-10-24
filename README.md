# Kalshi Scanner

Scans Kalshi's BTC 15-minute markets plus gold, silver, WTI oil and natural gas markets.
Every signal is paper-traded. **Live auto-trading unlocks per asset only after that asset
passes every approval gate**, and stays inside hard risk caps. Text alerts + black dashboard.

Not financial advice. Paper results can overstate real results.

## How it decides
1. Pulls open Kalshi markets closing within 60 minutes (public API, no key).
2. Gets the live underlying price (Coinbase for BTC; Yahoo futures for metals/energy).
3. Calculates fair odds from current price, strike, time left, and recent volatility.
4. Signal = fair odds beat the ask by at least `MIN_EDGE` after Kalshi fees and 1¢ slippage.
5. Rejects stale prices, thin books, and prices under 5¢ or over 90¢.

## Approval gates (all must pass, per asset)
| Gate | Default |
|---|---|
| Settled paper trades | ≥ 150 |
| Days tested | ≥ 7 |
| Net profit after fees | > $0 |
| Profit factor | ≥ 1.25 |
| Win rate beats market-implied odds | ≥ +3 points |
| Max drawdown (1-contract units) | ≤ $15 |
| Last 50 trades profitable | > $0 |

Gates are re-checked every cycle. An asset that starts failing drops back to paper automatically.

## Live safety
- `TRADING_MODE=live` **and** passing gates **and** API key **and** kill switch off.
- Immediate-or-cancel orders only (never rests on the book).
- Caps: $10 / 5 contracts per trade, 3 open positions, 20 trades/day, $25 daily loss.
- Auto kill switch on daily loss limit or 6 live losses in a row. Big red STOP button on the dashboard.
- Test first with `KALSHI_DEMO=true` (Kalshi demo exchange, fake money; needs a demo API key).

## Deploy on DigitalOcean
1. Create a GitHub repo (private) and push this folder.
2. Create an Ubuntu 24.04 droplet ($6/mo basic is enough).
3. SSH in as root:
   ```bash
   git clone https://github.com/<you>/kalshi-scanner.git && cd kalshi-scanner
   bash deploy/setup_droplet.sh
   nano /opt/kalshi-scanner/.env      # set DASH_PASSWORD, alerts, etc.
   systemctl restart kalshi-engine kalshi-dashboard
   ```
4. Open `http://<droplet-ip>:8080`.
5. Updates later: `bash /opt/kalshi-scanner/deploy/update.sh`
6. Logs: `journalctl -u kalshi-engine -f`

## Text alerts
- **Free:** Gmail → carrier text gateway. Set `ALERT_METHOD=email_sms`, a Gmail App Password, and
  `SMS_GATEWAY_TO` (Verizon `@vtext.com`, T-Mobile `@tmomail.net`). AT&T no longer supports this.
- **Any carrier:** Twilio (~1¢/text). Set `ALERT_METHOD=twilio` and the four `TWILIO_*` values.

## Going live (only after gates are green)
1. Kalshi → Account → API keys → create key, download the private key file.
2. Upload it to `/opt/kalshi-scanner/kalshi_key.pem` (`chmod 600`, owned by `scanner`).
3. Set `KALSHI_API_KEY_ID`, `TRADING_MODE=live`. Run a day with `KALSHI_DEMO=true` first.

## Google Sheets
Dashboard → **Download CSV** → Google Sheets → File → Import.

## Tests
`pip install -r requirements.txt pytest && python -m pytest -q`
