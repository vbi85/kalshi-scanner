"""Text alerts. Methods (ALERT_METHOD, comma-separated): console, email_sms, twilio.

email_sms: free. Sends a plain email through Gmail to your carrier's text gateway
           (e.g. 4145551234@vtext.com for Verizon, @tmomail.net for T-Mobile).
           AT&T shut its gateway down in 2025 -> AT&T users should use twilio.
twilio:    paid (~1 cent/text) but works on every carrier.
"""
import smtplib
import time
from collections import deque
from email.message import EmailMessage

import requests


class Alerter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last = {}            # key -> ts
        self.sent = deque()       # timestamps for hourly cap

    def _allowed(self, key):
        now = time.time()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        if len(self.sent) >= self.cfg.max_alerts_per_hour:
            return False
        if key and now - self.last.get(key, 0) < self.cfg.alert_cooldown_min * 60:
            return False
        return True

    def send(self, text, key=None, force=False):
        if not force and not self._allowed(key):
            return False
        self.last[key] = time.time()
        self.sent.append(time.time())
        text = text[:300]
        for m in self.cfg.alert_methods:
            try:
                getattr(self, f"_{m}")(text)
            except Exception as e:
                print(f"[alert] {m} failed: {e}")
        return True

    def _console(self, text):
        print(f"[ALERT] {text}")

    def _email_sms(self, text):
        c = self.cfg
        if not (c.smtp_user and c.smtp_password and c.sms_gateway_to):
            raise RuntimeError("SMTP_USER, SMTP_PASSWORD and SMS_GATEWAY_TO must be set")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = c.smtp_user, c.sms_gateway_to, ""
        msg.set_content(text)
        with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15) as s:
            s.starttls()
            s.login(c.smtp_user, c.smtp_password)
            s.send_message(msg)

    def _twilio(self, text):
        c = self.cfg
        r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{c.twilio_sid}/Messages.json",
                          auth=(c.twilio_sid, c.twilio_token),
                          data={"From": c.twilio_from, "To": c.twilio_to, "Body": text}, timeout=10)
        r.raise_for_status()
