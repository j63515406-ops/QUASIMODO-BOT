import logging
import time

import requests

import config

log = logging.getLogger("telegram")


def send(text):
    if not config.TG_TOKEN or not config.TG_CHAT:
        log.info("[TG sin configurar] %s", text.replace("\n", " | ")[:300])
        return
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage"
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        for attempt in range(3):
            try:
                r = requests.post(url, data={"chat_id": config.TG_CHAT, "text": chunk,
                                             "parse_mode": "HTML",
                                             "disable_web_page_preview": "true"}, timeout=15)
                if r.status_code == 429:
                    time.sleep(r.json().get("parameters", {}).get("retry_after", 3))
                    continue
                if r.ok:
                    break
                log.warning("Telegram %s %s", r.status_code, r.text[:200])
                break
            except Exception as e:  # noqa
                log.warning("Telegram error: %s", e)
                time.sleep(2)
