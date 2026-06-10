from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from app.config import normalize_coin
from typing import Any

CACHE_PATH = Path("data/news_risk_cache.json")

NEWS_SOURCES = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

ALIASES = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin", "binance"],
    "LINK": ["chainlink", "link"],
    "AAVE": ["aave"],
    "AVAX": ["avalanche", "avax"],
    "SUI": ["sui"],
    "NEAR": ["near protocol", "near"],
    "RENDER": ["render", "rndr", "render token"],
    "FET": ["fetch.ai", "fetch ai", "fet", "artificial superintelligence alliance", "asi"],
    "TON": ["toncoin", "ton", "telegram open network"],
}

NEGATIVE_KEYWORDS = {
    "hack": 20,
    "hacked": 20,
    "exploit": 20,
    "exploited": 20,
    "vulnerability": 16,
    "attack": 15,
    "sec": 14,
    "lawsuit": 18,
    "sues": 18,
    "investigation": 15,
    "probe": 14,
    "delist": 22,
    "delisting": 22,
    "halt": 16,
    "outage": 14,
    "downtime": 14,
    "bridge": 8,
    "rug": 30,
    "fraud": 28,
    "scam": 25,
    "unlock": 12,
    "token unlock": 18,
    "selloff": 12,
    "liquidation": 10,
    "ban": 18,
    "sanction": 18,
    "bankruptcy": 25,
}

POSITIVE_KEYWORDS = {
    "etf approval": -12,
    "partnership": -5,
    "upgrade": -5,
    "mainnet": -5,
    "integration": -4,
    "inflow": -4,
}


def _read_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fresh(row: dict, minutes: int = 30) -> bool:
    try:
        return datetime.now() - datetime.fromisoformat(row.get("ts", "")) < timedelta(minutes=minutes)
    except Exception:
        return False


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.isoformat(timespec="seconds")
    except Exception:
        return ""


def _fetch_rss_items(url: str, timeout: int = 8) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-invest-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml_text = resp.read().decode("utf-8", errors="ignore")
    root = ET.fromstring(xml_text)
    items = []
    channel_items = root.findall(".//item")
    for item in channel_items[:40]:
        title = _clean_html(item.findtext("title") or "")
        link = item.findtext("link") or ""
        desc = _clean_html(item.findtext("description") or "")
        pub = _parse_date(item.findtext("pubDate"))
        items.append({"title": title, "link": link, "summary": desc[:500], "published": pub, "source": url})
    return items


def fetch_news_items() -> list[dict]:
    cache = _read_cache()
    cached = cache.get("rss_items")
    if cached and _fresh(cached, 30):
        return cached["value"]

    all_items: list[dict] = []
    for url in NEWS_SOURCES:
        try:
            all_items.extend(_fetch_rss_items(url))
        except Exception:
            continue

    # дедуп по заголовку
    seen = set()
    unique = []
    for item in all_items:
        key = item.get("title", "").lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    cache["rss_items"] = {"ts": datetime.now().isoformat(timespec="seconds"), "value": unique[:80]}
    _write_cache(cache)
    return unique[:80]


def _mentions_coin(text: str, coin: str) -> bool:
    text_l = text.lower()
    for alias in ALIASES.get(coin.upper(), [coin.lower()]):
        # короткие тикеры проверяем как отдельное слово
        if len(alias) <= 4:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", text_l):
                return True
        elif alias.lower() in text_l:
            return True
    return False


def analyze_news_risk(coin: str) -> dict:
    coin = normalize_coin(coin)
    cache = _read_cache()
    key = f"risk_{coin}"
    cached = cache.get(key)
    if cached and _fresh(cached, 30):
        return cached["value"]

    try:
        items = fetch_news_items()
    except Exception:
        items = []

    relevant = []
    risk_score = 0
    positive_adj = 0
    hits: list[str] = []

    for item in items:
        text = f"{item.get('title','')} {item.get('summary','')}"
        if not _mentions_coin(text, coin):
            continue
        text_l = text.lower()
        local_score = 0
        local_hits = []
        for kw, weight in NEGATIVE_KEYWORDS.items():
            if kw in text_l:
                local_score += weight
                local_hits.append(kw)
        for kw, weight in POSITIVE_KEYWORDS.items():
            if kw in text_l:
                positive_adj += abs(weight)
                local_score += weight
                local_hits.append(kw)
        relevant.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "published": item.get("published", ""),
            "hits": local_hits[:5],
        })
        if local_score > 0:
            risk_score += local_score
            hits.extend(local_hits)

    risk_score = max(0, min(100, int(risk_score)))
    if risk_score >= 45:
        state = "HIGH"
        comment = "новостной риск высокий — ручная проверка обязательна"
        score_adj = -20
    elif risk_score >= 22:
        state = "ELEVATED"
        comment = "есть тревожные новости — вход уменьшить или ждать"
        score_adj = -10
    elif relevant:
        state = "LOW"
        comment = "сильного негатива в свежих RSS не найдено"
        score_adj = 2
    else:
        state = "UNKNOWN"
        comment = "свежих новостей по монете не найдено"
        score_adj = 0

    result = {
        "coin": coin,
        "state": state,
        "risk_score": risk_score,
        "score_adj": score_adj,
        "comment": comment,
        "hits": sorted(set(hits))[:8],
        "items": relevant[:5],
        "source": "RSS: Cointelegraph/Decrypt/CoinDesk",
    }
    cache[key] = {"ts": datetime.now().isoformat(timespec="seconds"), "value": result}
    _write_cache(cache)
    return result


def short_news_text(risk: dict | None) -> str:
    if not risk:
        return "новости: нет данных"
    return f"новости: {risk.get('state','UNKNOWN')} — {risk.get('comment','')}"
