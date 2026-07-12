#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 開源日報產生器
==================
流程：
  1. GitHub Search API 抓「近 N 天新建立、star 快速累積」的 AI 專案
  2. 解析 github.com/trending 頁面抓「今日竄升」專案（官方 API 無 trending 端點，故採爬蟲近似）
  3. 合併去重、抓取各專案 README 摘錄
  4. 丟給 Claude API 撰寫繁體中文報紙式日報
  5. 存檔到 digests/YYYY-MM-DD.md，並透過 Gmail SMTP 寄出

環境變數（於 GitHub repo → Settings → Secrets and variables → Actions 設定）：
  ANTHROPIC_API_KEY    必填（--dry-run 模式除外）
  GMAIL_ADDRESS        寄件 Gmail 帳號（選填；未設定則只存檔、不寄信）
  GMAIL_APP_PASSWORD   Gmail「應用程式密碼」（選填，需搭配上者）
  RECIPIENT_EMAIL      收件人（選填，預設寄給 GMAIL_ADDRESS 自己）
  GITHUB_TOKEN         GitHub Actions 會自動注入，可提高 API 速率上限

可調參數（環境變數，皆有預設值）：
  DAYS_BACK=7          「新專案」回看天數
  MIN_STARS=80         新專案最低 star 門檻
  MAX_REPOS=10         日報最多收錄專案數
  TOPICS=ai,llm,...    要掃描的 GitHub topics（逗號分隔）
  CLAUDE_MODEL         預設 claude-sonnet-4-6

用法：
  python digest.py            # 完整流程
  python digest.py --dry-run  # 只抓取與篩選專案並列印，不呼叫 Claude、不寄信（測試用）
"""

import base64
import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

GITHUB_API = "https://api.github.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
TAIPEI = dt.timezone(dt.timedelta(hours=8))

CONFIG = {
    "days_back": int(os.environ.get("DAYS_BACK", "7")),
    "min_stars": int(os.environ.get("MIN_STARS", "80")),
    "max_repos": int(os.environ.get("MAX_REPOS", "10")),
    "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
    "topics": [
        t.strip()
        for t in os.environ.get(
            "TOPICS", "ai,llm,agents,rag,generative-ai,machine-learning"
        ).split(",")
        if t.strip()
    ],
    "readme_chars": 2500,  # 每個專案 README 摘錄長度上限
}

# 用來從 trending 頁面過濾出 AI 相關專案的關鍵字（名稱 + 描述比對）
AI_PATTERN = re.compile(
    r"\b(ai|llm|llms|gpt|agent|agents|agentic|rag|nlp|genai|ocr|tts|asr)\b"
    r"|machine.?learning|deep.?learning|neural|transformer|diffusion"
    r"|language model|multimodal|embedding|inference|fine.?tun|chatbot"
    r"|copilot|text.?to.?(image|speech|video|sql)|speech|voice"
    r"|openai|anthropic|claude|gemini|llama|mistral|deepseek|qwen"
    r"|stable.?diffusion|vision model|prompt",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(f"[digest] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. 資料來源：GitHub Search API（新星專案）
# ---------------------------------------------------------------------------
def github_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-daily-digest-bot",
        }
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def search_new_repos(session: requests.Session) -> dict:
    """以 topic 搜尋近 N 天建立、star 數達門檻的專案。回傳 {full_name: repo_dict}"""
    since = (dt.datetime.now(TAIPEI) - dt.timedelta(days=CONFIG["days_back"])).date()
    found: dict = {}
    for topic in CONFIG["topics"]:
        q = f"topic:{topic} created:>{since} stars:>{CONFIG['min_stars']}"
        try:
            r = session.get(
                f"{GITHUB_API}/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 15},
                timeout=30,
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            log(f"search topic:{topic} → {len(items)} 筆")
            for it in items:
                fn = it["full_name"]
                if fn not in found:
                    found[fn] = {
                        "full_name": fn,
                        "html_url": it["html_url"],
                        "description": it.get("description") or "",
                        "stars": it.get("stargazers_count", 0),
                        "created_at": it.get("created_at", ""),
                        "language": it.get("language") or "",
                        "topics": it.get("topics", []),
                        "stars_today": None,
                        "source": "new",
                    }
        except requests.RequestException as e:
            log(f"search topic:{topic} 失敗（略過）：{e}")
        # Search API 速率限制：未驗證 10 次/分、驗證 30 次/分，保守間隔
        time.sleep(2.5)
    return found


# ---------------------------------------------------------------------------
# 2. 資料來源：github.com/trending 頁面（爬蟲，官方 API 無此端點）
# ---------------------------------------------------------------------------
def scrape_trending(session: requests.Session) -> list:
    """解析 trending 頁面。頁面結構若改版會失敗，故整段包在 try 內、失敗不中斷主流程。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("未安裝 beautifulsoup4，略過 trending 來源")
        return []
    results = []
    try:
        r = requests.get(
            "https://github.com/trending?since=daily",
            headers={"User-Agent": "Mozilla/5.0 (ai-daily-digest-bot)"},
            timeout=30,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("article.Box-row")
        log(f"trending 頁面解析到 {len(rows)} 個專案")
        for row in rows:
            a = row.select_one("h2 a") or row.select_one("h1 a")
            if not a or not a.get("href"):
                continue
            full_name = a["href"].strip("/")
            desc_el = row.select_one("p")
            desc = desc_el.get_text(strip=True) if desc_el else ""
            stars_today = None
            for span in row.select("span"):
                text = span.get_text(strip=True)
                if "stars today" in text:
                    m = re.search(r"([\d,]+)", text)
                    if m:
                        stars_today = int(m.group(1).replace(",", ""))
                    break
            # 只留 AI 相關
            if AI_PATTERN.search(f"{full_name} {desc}"):
                results.append(
                    {"full_name": full_name, "description": desc, "stars_today": stars_today}
                )
    except Exception as e:  # 爬蟲屬 best-effort，任何錯誤都不該讓日報開天窗
        log(f"trending 爬取失敗（略過此來源）：{e}")
    return results


def enrich_repo(session: requests.Session, full_name: str) -> dict | None:
    """用官方 API 補齊 trending 專案的中繼資料"""
    try:
        r = session.get(f"{GITHUB_API}/repos/{full_name}", timeout=30)
        r.raise_for_status()
        it = r.json()
        return {
            "full_name": full_name,
            "html_url": it["html_url"],
            "description": it.get("description") or "",
            "stars": it.get("stargazers_count", 0),
            "created_at": it.get("created_at", ""),
            "language": it.get("language") or "",
            "topics": it.get("topics", []),
        }
    except requests.RequestException as e:
        log(f"補齊 {full_name} 失敗：{e}")
        return None


# ---------------------------------------------------------------------------
# 3. 合併、排序、抓 README
# ---------------------------------------------------------------------------
def collect(session: requests.Session) -> list:
    repos = search_new_repos(session)
    for t in scrape_trending(session):
        fn = t["full_name"]
        if fn in repos:
            repos[fn]["stars_today"] = t["stars_today"]
            repos[fn]["source"] = "trending+new"
        else:
            meta = enrich_repo(session, fn)
            if meta is None:
                # API 補齊失敗（如速率限制）時，退回使用爬蟲已取得的資料，不丟棄專案
                meta = {
                    "full_name": fn,
                    "html_url": f"https://github.com/{fn}",
                    "description": t.get("description", ""),
                    "stars": 0,  # 0 代表未知，交由日報註明
                    "created_at": "",
                    "language": "",
                    "topics": [],
                }
            meta["stars_today"] = t["stars_today"]
            meta["source"] = "trending"
            repos[fn] = meta
            time.sleep(1.0)

    # 排序：今日竄升（trending）優先、依 stars_today 高到低；其後為新專案依總 star 數
    def sort_key(r):
        is_trending = "trending" in r["source"]
        return (
            0 if is_trending else 1,
            -(r.get("stars_today") or 0),
            -r.get("stars", 0),
        )

    ranked = sorted(repos.values(), key=sort_key)

    # 版面配額：trending（今日竄升）最多佔六成，其餘名額留給近 N 天的新星專案，
    # 避免單一來源壟斷版面；若某一來源不足額，由另一來源回補。
    trending = [r for r in ranked if "trending" in r["source"]]
    fresh = [r for r in ranked if r["source"] == "new"]
    cap = max(1, round(CONFIG["max_repos"] * 0.6))
    selected = trending[:cap]
    selected += fresh[: CONFIG["max_repos"] - len(selected)]
    if len(selected) < CONFIG["max_repos"]:  # 新星不足額 → trending 回補
        selected += trending[cap : cap + CONFIG["max_repos"] - len(selected)]
    return selected


def fetch_readme(session: requests.Session, full_name: str) -> str:
    try:
        r = session.get(
            f"{GITHUB_API}/repos/{full_name}/readme",
            headers={"Accept": "application/vnd.github.raw+json"},
            timeout=30,
        )
        r.raise_for_status()
        text = r.text
        # 去掉徽章/圖片行與多餘空行，節省 token
        lines = [
            ln
            for ln in text.splitlines()
            if not ln.strip().startswith(("[![", "![", "<img", "<p align"))
        ]
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return cleaned[: CONFIG["readme_chars"]]
    except requests.RequestException:
        return ""


# ---------------------------------------------------------------------------
# 4. Claude 撰寫日報
# ---------------------------------------------------------------------------
EDITOR_SYSTEM_PROMPT = """你是《AI 開源日報》的主編，這是一份給資深工程師看的繁體中文日報。
讀者背景：會寫 Python、懂軟體工程、重視實用性、對浮誇的行銷語言免疫。

寫作要求：
- 全文繁體中文（台灣用語），專有名詞與專案名稱保留英文
- 語氣像財經報紙的科技版：克制、有觀點、不吹捧
- 對每個專案誠實評估成熟度（例如：專案剛起步、文件不足、star 暴漲可能來自行銷聲量）
- 只根據提供的資料撰寫；若資料不足以判斷，直說「僅從 README 無法確認」，嚴禁腦補功能

輸出格式（純 Markdown，開頭不要有任何說明文字）：

# AI 開源日報
**{date}｜共 {n} 條精選**

## 📌 頭版焦點
（從清單中挑「一個」最重要的專案，用 150–250 字說明它是什麼、為什麼重要、對生態系可能的影響）

## 今日精選
（其餘每個專案一節，格式如下）
### [owner/repo](連結)　⭐ 總星數（今日 +N，若有資料）
- **一句話定位**：
- **能拿來做什麼**：
- **編輯點評**：（與同類工具的差異、成熟度判斷、風險或限制）

## 收盤註記
（2–3 句話，總結今天觀察到的趨勢，可以有立場但要有根據）
"""


def call_claude(repos: list, date_str: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少 ANTHROPIC_API_KEY，無法產生日報（測試請用 --dry-run）")
    payload_repos = [
        {
            "full_name": r["full_name"],
            "url": r["html_url"],
            "stars": r["stars"],
            "stars_today": r.get("stars_today"),
            "created_at": r.get("created_at", ""),
            "language": r.get("language", ""),
            "topics": r.get("topics", []),
            "description": r.get("description", ""),
            "readme_excerpt": r.get("readme", ""),
        }
        for r in repos
    ]
    user_msg = (
        f"今天是 {date_str}。以下是今日候選專案資料（JSON），請依系統指示撰寫日報：\n\n"
        + json.dumps(payload_repos, ensure_ascii=False, indent=1)
    )
    r = requests.post(
        ANTHROPIC_API,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CONFIG["model"],
            "max_tokens": 4096,
            "system": EDITOR_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=300,
    )
    r.raise_for_status()
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text.strip():
        raise SystemExit(f"Claude 回應為空，原始回應：{json.dumps(data)[:500]}")
    return text.strip()


# ---------------------------------------------------------------------------
# 5. 存檔與寄信
# ---------------------------------------------------------------------------
def save_digest(markdown_text: str, date_str: str) -> str:
    os.makedirs("digests", exist_ok=True)
    path = os.path.join("digests", f"{date_str}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown_text + "\n")
    log(f"日報已存檔：{path}")
    return path


def markdown_to_html(markdown_text: str) -> str:
    try:
        import markdown as md

        body = md.markdown(markdown_text, extensions=["extra"])
    except ImportError:
        body = f"<pre>{markdown_text}</pre>"
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f0;">
<div style="max-width:680px;margin:0 auto;padding:24px;background:#ffffff;
            font-family:-apple-system,'Noto Sans TC','Microsoft JhengHei',sans-serif;
            font-size:15px;line-height:1.75;color:#1f2328;">
{body}
<hr style="border:none;border-top:1px solid #ddd;margin-top:32px;">
<p style="color:#888;font-size:12px;">本報由 GitHub Actions 自動產生，資料來源：GitHub Search API 與 github.com/trending。</p>
</div></body></html>"""


def send_email(markdown_text: str, date_str: str, n_repos: int) -> None:
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip() or sender
    if not sender or not password:
        log("未設定 GMAIL_ADDRESS / GMAIL_APP_PASSWORD，跳過寄信（日報僅存檔）")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(f"AI 開源日報 · {date_str} · 今日 {n_repos} 條精選", "utf-8")
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(markdown_text, "plain", "utf-8"))
    msg.attach(MIMEText(markdown_to_html(markdown_text), "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())
    log(f"日報已寄出 → {recipient}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    dry_run = "--dry-run" in sys.argv
    date_str = dt.datetime.now(TAIPEI).strftime("%Y-%m-%d")
    log(f"開始產生 {date_str} 日報（dry-run={dry_run}）")

    session = github_session()
    repos = collect(session)
    if not repos:
        log("今日沒有符合條件的專案，不產生日報。")
        return

    log(f"共選入 {len(repos)} 個專案：")
    for r in repos:
        today = f"（今日 +{r['stars_today']}）" if r.get("stars_today") else ""
        log(f"  [{r['source']:>12}] ⭐{r['stars']:>6} {today:<12} {r['full_name']}")

    if dry_run:
        log("dry-run 結束，不呼叫 Claude、不寄信。")
        return

    for r in repos:
        r["readme"] = fetch_readme(session, r["full_name"])
        time.sleep(0.5)

    digest_md = call_claude(repos, date_str)
    save_digest(digest_md, date_str)
    send_email(digest_md, date_str, len(repos))
    log("完成。")


if __name__ == "__main__":
    main()
