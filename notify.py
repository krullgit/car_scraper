#!/usr/bin/env python3
"""Telegram notifications for new vehicles."""

import requests

import config
import secrets as secrets_mod


def _credentials() -> tuple[str, str]:
    token = secrets_mod.get_secret("TELEGRAM_TOKEN")
    chat_id = secrets_mod.get_secret("TELEGRAM_CHAT_ID")
    return token, chat_id


def send_telegram(message: str) -> None:
    token, chat_id = _credentials()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def notify_new_cars(conn, new_ids: set[int]) -> None:
    if not new_ids:
        return
    conn.execute("CREATE TABLE IF NOT EXISTS car_notified (vehicleid INTEGER PRIMARY KEY)")
    already = {r[0] for r in conn.execute(
        f"SELECT vehicleid FROM car_notified WHERE vehicleid IN ({','.join('?' for _ in new_ids)})",
        list(new_ids),
    ).fetchall()}
    to_notify = new_ids - already
    if not to_notify:
        return
    rows = conn.execute(
        f"SELECT shortdescription, customerprice, kilometers, registrationdate, dealerid, vehicleid "
        f"FROM cars WHERE vehicleid IN ({','.join('?' for _ in to_notify)}) "
        f"ORDER BY customerprice",
        list(to_notify),
    ).fetchall()

    for r in rows:
        price = f"{r['customerprice']:,.0f}€".replace(",", ".")
        km = f"{r['kilometers']:,d}".replace(",", ".")
        reg = (r["registrationdate"] or "")[:7]
        # No dealer-name / location interpretation — only the raw dealer id.
        dname = f"Dealer {r['dealerid']}"
        vid = r["vehicleid"]
        url = f"{config.BASE_URL}/{vid}"
        msg = (
            f"🆕 <b>Neues E-Fahrzeug!</b>\n"
            f"{r['shortdescription']}\n"
            f"💰 {price} · {km}km · EZ {reg}\n"
            f"📍 {dname}\n"
            f"<a href=\"{url}\">Zum Angebot</a>\n"
            f"🖥 <a href=\"http://192.168.0.13:8080\">Trader ansehen</a>"
        )
        send_telegram(msg)
        conn.execute("INSERT OR IGNORE INTO car_notified (vehicleid) VALUES (?)", (vid,))
    conn.commit()