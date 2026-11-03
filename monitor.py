#!/usr/bin/env python3
"""Shopee Taiwan restock monitor with Bark push notifications.

Uses publicly rendered product page data when available. Does not log in,
solve challenges, or use unofficial checkout APIs. Unknown = never restock.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
LOG_FILE = BASE / "monitor.log"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass(frozen=True)
class Result:
    state: str  # in_stock / out_of_stock / unknown
    reason: str
    title: str = ""


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必須是 JSON 物件")
    return value


def save_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_product_ids(url: str) -> tuple[str, str]:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not (host == "shopee.tw" or host.endswith(".shopee.tw")):
        raise ValueError("商品網址必須是 https://shopee.tw/ 的台灣蝦皮連結")
    patterns = (r"-i\.(\d+)\.(\d+)(?:/|$)", r"/product/(\d+)/(\d+)(?:/|$)", r"/opaanlp/(\d+)/(\d+)(?:/|$)", r"/i\.(\d+)\.(\d+)(?:/|$)")
    for pattern in patterns:
        m = re.search(pattern, p.path, flags=re.IGNORECASE)
        if m:
            return m.group(1), m.group(2)
    raise ValueError("無法從網址找到賣家/商品 ID；請使用包含 -i.賣家ID.商品ID 或 /product/賣家ID/商品ID 的完整網址")


def extract_payload(html: str, shop_id: str, item_id: str) -> Optional[dict]:
    """Decode the page's embedded PDP object; never use masked stock count."""
    key = re.compile(r'"' + re.escape(shop_id + "/" + item_id) + r'"\s*:\s*(?=\{)')
    decoder = json.JSONDecoder()
    for match in key.finditer(html):
        try:
            value, _ = decoder.raw_decode(html[match.end():])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("item"), dict):
            return value
    return None


def model_state(model: dict) -> Optional[bool]:
    """Tri-state model buyability. Contradictory/missing flags are unknown."""
    gray = model.get("is_grayout")
    click = model.get("is_clickable")
    if not isinstance(gray, bool):
        gray = None
    if not isinstance(click, bool):
        click = None
    if gray is True and click is True:
        return None
    if gray is True or click is False:
        return False
    if gray is False or click is True:
        return True
    return None


def inspect_payload(payload: dict, keywords: list[str]) -> Result:
    item = payload.get("item")
    if not isinstance(item, dict):
        return Result("unknown", "無法辨識商品資料")
    title = str(item.get("title") or item.get("name") or "")
    if item.get("item_status") != "normal" or item.get("status") != 1:
        return Result("unknown", "商品可能未上架／暫時無法販售，不能視為缺貨", title)
    models = item.get("models")
    if not isinstance(models, list) or not models:
        return Result("unknown", "找不到可判斷的商品規格資訊", title)
    options = [m for m in models if isinstance(m, dict)]
    if keywords:
        terms = [str(word).strip().casefold() for word in keywords if str(word).strip()]
        options = [m for m in options if all(t in str(m.get("name", "")).casefold() for t in terms)]
        if not options:
            examples = "、".join(str(m.get("name", "")) for m in models[:6] if isinstance(m, dict))
            return Result("unknown", f"找不到指定規格 {keywords}；目前規格：{examples}", title)
    states = [model_state(m) for m in options]
    if True in states:
        names = [str(m.get("name") or "單一規格") for m, ok in zip(options, states) if ok]
        return Result("in_stock", "可購買規格：" + "、".join(names[:5]), title)
    if states and all(x is False for x in states):
        return Result("out_of_stock", "指定規格目前不可購買", title)
    return Result("unknown", "頁面未提供一致、明確的可購買狀態", title)


def detect_interstitial(html: str) -> bool:
    body = html[:50000].lower()
    terms = ["captcha", "請下載並登入蝦皮購物 app 以繼續使用", "請登入蝦皮購物app", "verify you are human"]
    return any(term in body for term in terms)


def inspect_browser_controls(snapshot: dict) -> Result:
    """Conservative DOM fallback. Never treat missing controls as a stock-out.

    Real-world Shopee markup may differ; a positive result still needs live
    validation against this particular product before relying on alerts.
    """
    title = str(snapshot.get("product_title") or "").strip()
    if snapshot.get("blocked"):
        return Result("unknown", "瀏覽器被要求登入／進行驗證；不繞過限制")
    if not title or title.casefold() in {"shopee", "蝦皮購物", "shopee taiwan"}:
        return Result("unknown", "網頁未呈現可確認的商品標題")

    controls = snapshot.get("controls") or []
    purchase_terms = ("加入購物車", "加到購物車", "直接購買", "立即購買", "add to cart", "buy now")
    sold_terms = ("已售完", "已售罄", "售完", "缺貨", "補貨中", "sold out", "out of stock")
    purchase = []
    sold_out = []
    for control in controls:
        if not isinstance(control, dict) or not control.get("visible"):
            continue
        text = str(control.get("text", "")).strip().casefold()
        if len(text) > 40:  # Ignore huge containers that wrap unrelated products.
            continue
        if any(t in text for t in sold_terms):
            sold_out.append(control)
        if any(t in text for t in purchase_terms):
            purchase.append(control)

    if sold_out and not any(c.get("disabled") is False for c in purchase):
        return Result("out_of_stock", "商品購買操作顯示已售完／缺貨", title)
    if purchase and any(c.get("disabled") is False for c in purchase) and not sold_out:
        return Result("in_stock", "商品購買操作可使用（DOM 備援）", title)
    if purchase and all(c.get("disabled") is True for c in purchase):
        return Result("unknown", "購買按鈕不可使用，但沒有明確缺貨文字，無法確定是缺貨或登入／配送限制", title)
    return Result("unknown", "網頁已載入，但沒有可確認的購買按鈕狀態", title)


def browser_inspect(url: str, shop_id: str, item_id: str) -> Result:
    """Only inspect the rendered page. No login, clicking, or CAPTCHA bypass."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return Result("unknown", "尚未安裝 Playwright，無法使用瀏覽器備援")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="zh-TW")
                response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(3500)
                html = page.content()
                if detect_interstitial(html):
                    return Result("unknown", "瀏覽器遇到驗證／登入要求")
                payload = extract_payload(html, shop_id, item_id)
                if payload is not None:
                    parsed = inspect_payload(payload, [])
                    if parsed.state != "unknown":
                        return parsed
                    logging.info("嵌入式商品資料仍無法判定：%s；改檢查可見購買按鈕", parsed.reason)
                snapshot = page.evaluate("""() => {
                    const visible = el => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && rect.top > -200 && rect.top < innerHeight * 1.5 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const controls = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'))
                        .filter(visible)
                        .map(el => ({
                            text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 90),
                            disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true' || el.closest('[aria-disabled="true"]')),
                            visible: true
                        }))
                        .filter(b => /加入購物車|加到購物車|直接購買|立即購買|已售完|已售罄|售完|缺貨|補貨中|add to cart|buy now|sold out|out of stock/i.test(b.text))
                        .slice(0, 15);
                    const meta = document.querySelector('meta[property="og:title"]')?.content || '';
                    const heading = document.querySelector('h1')?.innerText || '';
                    return {
                        product_title: (meta || heading).trim().slice(0, 140),
                        page_title: document.title.slice(0, 140),
                        controls,
                        blocked: /captcha|verify you are human|請下載並登入蝦皮購物\\s*app|請登入蝦皮購物app/i.test((document.body?.innerText || '').slice(0, 5000))
                    };
                }""")
                # Public, short diagnostics only; don't print arbitrary page content.
                logging.info("瀏覽器診斷：標題=%r，相關購買按鈕=%s，HTTP=%s",
                             snapshot.get('product_title', '')[:65],
                             snapshot.get('controls', [])[:6],
                             getattr(response, 'status', '?'))
                return inspect_browser_controls(snapshot)
            finally:
                browser.close()
    except Exception as exc:
        return Result("unknown", "瀏覽器讀取失敗：" + str(exc)[:140])


def fetch_stock(session: requests.Session, item: dict, use_browser: bool) -> Result:
    url = str(item["url"])
    try:
        shop_id, item_id = parse_product_ids(url)
    except ValueError as exc:
        return Result("unknown", str(exc))

    payload = None
    try:
        resp = session.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        if resp.status_code in (403, 429):
            return Result("unknown", f"網站拒絕請求（HTTP {resp.status_code}），不嘗試繞過限制")
        resp.raise_for_status()
        if detect_interstitial(resp.text):
            return Result("unknown", "蝦皮要求驗證或登入，無法判斷")
        payload = extract_payload(resp.text, shop_id, item_id)
    except requests.RequestException as exc:
        return Result("unknown", "網路或網站錯誤：" + str(exc)[:160])

    keywords = item.get("variant_keywords", [])
    if not isinstance(keywords, list):
        return Result("unknown", "variant_keywords 必須是文字陣列")
    parsed = inspect_payload(payload, keywords) if payload is not None else None
    if parsed is not None and parsed.state != "unknown":
        return parsed
    if use_browser:
        if keywords:
            return parsed or Result("unknown", "指定規格需要結構化資料；DOM 備援無法安全確認指定規格")
        return browser_inspect(url, shop_id, item_id)
    return parsed or Result("unknown", "頁面找不到可辨識的庫存結構")


def send_bark(config: dict, title: str, body: str, url: str = "") -> None:
    key = str(config.get("bark_key") or "").strip()
    server = str(config.get("bark_server") or "https://api.day.app").rstrip("/")
    if not key or key.startswith("在這裡"):
        raise ValueError("尚未在 config.json 設定 bark_key")
    if not server.startswith("https://"):
        raise ValueError("bark_server 必須使用 https://")
    data = {"device_key": key, "title": title, "body": body, "group": "蝦皮補貨"}
    if url:
        data["url"] = url
    response = requests.post(server + "/push", json=data, timeout=15)
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Bark 回應不是 JSON，請檢查伺服器網址") from exc
    if result.get("code") not in (200,):
        raise RuntimeError("Bark 沒有確認發送成功：" + str(result.get("message", result))[:160])


def validate_config(config: dict) -> None:
    interval = config.get("interval_seconds", 600)
    if not isinstance(interval, (int, float)) or interval < 600:
        raise ValueError("interval_seconds 不得低於 600 秒（10 分鐘）")
    items = config.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("config.json 的 items 至少要有一個商品")
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict) or not item.get("url"):
            raise ValueError(f"第 {idx} 個商品沒有正確設定 url")
        if not isinstance(item.get("variant_keywords", []), list):
            raise ValueError(f"第 {idx} 個商品的 variant_keywords 必須是陣列")
        parse_product_ids(str(item["url"]))


def check_all(config: dict, session: requests.Session, *, use_browser: bool, dry_run: bool = False) -> int:
    """Return number of unknown stock checks, preserving confirmed stock states."""
    state = load_json(STATE_FILE, {}) if not dry_run else {}
    changed = False
    unknown_count = 0
    for item in config["items"]:
        url = str(item["url"])
        words = item.get("variant_keywords", [])
        key = url.split("?")[0] + "::" + json.dumps(words, ensure_ascii=False)
        health_key = "__health__::" + key
        label = str(item.get("name") or "蝦皮商品")
        result = fetch_stock(session, item, use_browser)
        logging.info("%s | %s | %s", label, result.state, result.reason)
        if dry_run:
            if result.state == "unknown":
                unknown_count += 1
            continue
        if result.state == "unknown":
            unknown_count += 1
            health = state.get(health_key, {})
            if not isinstance(health, dict):
                health = {}
            count = int(health.get("failures", 0)) + 1
            alerted = bool(health.get("alerted", False))
            threshold = max(1, int(config.get("unknown_alert_after", 3)))
            if count >= threshold and not alerted:
                try:
                    send_bark(config, "⚠️ 蝦皮庫存監控異常", f"{label} 已連續 {count} 次無法讀取庫存；補貨通知暫時不可靠。\n{result.reason}", url)
                    alerted = True
                    logging.warning("已透過 Bark 提醒庫存辨識異常")
                except Exception as exc:
                    logging.error("異常推播失敗，下次會重試：%s", str(exc)[:160])
            state[health_key] = {"failures": count, "alerted": alerted}
            changed = True
            continue
        if health_key in state:
            del state[health_key]
            changed = True
        previous = state.get(key)
        if previous == "out_of_stock" and result.state == "in_stock":
            message = f"{result.title or label}\n{result.reason}\n點擊查看商品"
            try:
                send_bark(config, "🛒 蝦皮商品補貨了！", message, url)
                logging.info("✅ 補貨推播成功：%s", label)
            except Exception as exc:
                logging.error("補貨通知失敗，保留缺貨狀態以便下次重試：%s", str(exc)[:200])
                continue
        elif previous is None:
            logging.info("首次檢查建立基準狀態（不發補貨通知）：%s", label)
        if previous != result.state:
            state[key] = result.state
            changed = True
    if changed:
        save_json(STATE_FILE, state)
    return unknown_count


def main() -> int:
    parser = argparse.ArgumentParser(description="蝦皮補貨監控 + Bark iPhone 推播")
    parser.add_argument("--test-push", action="store_true", help="只測試 Bark，不監控庫存")
    parser.add_argument("--once", action="store_true", help="只檢查一次後結束")
    parser.add_argument("--diagnose", action="store_true", help="只檢查並顯示狀態，不修改紀錄、不推播")
    parser.add_argument("--no-browser", action="store_true", help="不使用 Playwright 備援")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    try:
        config = load_json(CONFIG_FILE, {})
        if args.test_push:
            send_bark(config, "✅ Bark 測試成功", "蝦皮補貨監控已連接你的 iPhone。")
            print("已提交測試推播，請檢查 iPhone 通知。")
            return 0
        validate_config(config)
    except Exception as exc:
        logging.error("設定錯誤：%s", exc)
        return 2

    logging.info("開始監控 %d 個商品。每 %s 秒檢查（附少量浮動）。Ctrl+C 停止。", len(config["items"]), config.get("interval_seconds", 600))
    session = requests.Session()
    try:
        while True:
            try:
                check_all(config, session, use_browser=not args.no_browser, dry_run=args.diagnose)
            except Exception as exc:
                logging.exception("本次檢查失敗（不修改庫存狀態）：%s", exc)
            if args.once or args.diagnose:
                break
            time.sleep(float(config.get("interval_seconds", 600)) + random.uniform(0, 30))
    except KeyboardInterrupt:
        logging.info("監控已停止")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
