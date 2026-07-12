# AI 開源日報（ai-daily-digest）

每天早上 07:00（台灣時間）自動寄一份繁體中文日報到你的信箱，
內容是 GitHub 上近期竄升的 AI 開源專案，含頭版焦點、逐項點評與趨勢註記。
日報同時會以 Markdown 存檔在 `digests/` 資料夾，形成可回溯的報紙檔案庫。

## 架構

```
GitHub Actions（每日 cron）
  └─ digest.py
       ├─ ① GitHub Search API：近 7 天新建立、star>80 的 AI 專案（本週新星）
       ├─ ② github.com/trending 頁面：今日竄升專案（爬蟲；官方 API 無此端點）
       ├─ ③ 合併去重、trending 最多佔六成版面、抓各專案 README 摘錄
       ├─ ④ Claude API：撰寫繁中報紙式日報（主編 prompt 內建反吹捧規則）
       └─ ⑤ Gmail SMTP 寄出 + commit 到 digests/ 存檔
```

## 部署步驟（一次性，約 15 分鐘）

### 1. 建立 GitHub repo 並上傳

在你的帳號（如 YJ-chen0830）建立新 repo（公開或私有皆可），把本資料夾
四個檔案依原路徑上傳：

```
.github/workflows/daily-digest.yml
digest.py
requirements.txt
README.md
```

或用命令列：

```bash
cd ai-daily-digest
git init && git add . && git commit -m "init"
git remote add origin git@github.com:你的帳號/ai-daily-digest.git
git push -u origin main
```

### 2. 取得兩組憑證

**Anthropic API Key**
到 console.anthropic.com → API Keys → Create Key。需綁定付款方式，
每日費用估算見下方。

**Gmail 應用程式密碼**（不是你的 Gmail 登入密碼）
1. Google 帳戶必須先開啟兩步驟驗證
2. 到 myaccount.google.com/apppasswords
3. 建立一組應用程式密碼（16 碼），複製起來

### 3. 設定 Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret，
共設定三～四筆：

| Secret 名稱 | 內容 | 必填 |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` | ✅ |
| `GMAIL_ADDRESS` | 你的 Gmail 帳號 | ✅（要寄信的話） |
| `GMAIL_APP_PASSWORD` | 16 碼應用程式密碼 | ✅（同上） |
| `RECIPIENT_EMAIL` | 收件人 | 選填，預設寄給自己 |

（`GITHUB_TOKEN` 不用設定，Actions 會自動注入。）

### 4. 手動測試一次

Repo → Actions → 左側「AI Daily Digest」→ 右側「Run workflow」。
約 2–3 分鐘後應收到第一份日報，同時 `digests/` 會多出當天的 `.md` 檔。
若失敗，點進該次執行看紅字步驟的 log。

之後每天台灣時間早上 7 點左右會自動寄送。工作流程失敗時
GitHub 預設會寄通知信給你，所以「沒收到日報」不會無聲無息。

## 客製化

在 `daily-digest.yml` 的 `env:` 區塊解除註解即可調整：

| 變數 | 預設 | 說明 |
|---|---|---|
| `DAYS_BACK` | 7 | 「新星」回看天數 |
| `MIN_STARS` | 80 | 新專案最低 star 門檻 |
| `MAX_REPOS` | 10 | 日報收錄上限 |
| `TOPICS` | ai,llm,agents,rag,generative-ai,machine-learning | 掃描的 topics，逗號分隔 |
| `CLAUDE_MODEL` | claude-sonnet-4-6 | 摘要模型 |

改發報時間：修改 cron（**UTC 時間**，台灣時間減 8 小時）。
例如台灣 21:00 → `'0 13 * * *'`。

改編輯口味：直接改 `digest.py` 裡的 `EDITOR_SYSTEM_PROMPT`，
例如要求多關注工程計算類、agent 框架，或改成週報彙整風格。

本機測試（不花錢、不寄信）：

```bash
pip install -r requirements.txt
python digest.py --dry-run
```

## 費用與額度

- **GitHub Actions**：公開 repo 免費；私有 repo 每月 2,000 分鐘免費額度，
  本任務每日約 2–3 分鐘，用量約 4–5%，綽綽有餘。
- **Claude API**：每日一次呼叫，輸入約 1.5–2 萬 tokens（10 份 README 摘錄）、
  輸出約 2 千 tokens。以 Sonnet 級模型計價約 US$0.05–0.10／天，
  即每月約 US$1.5–3（新台幣五十到一百元之譜）。
- **Gmail SMTP**：免費，個人帳號每日 500 封上限，日報一封無虞。

## 已知限制與設計假設

1. **Trending 無官方 API**：`github.com/trending` 是爬蟲解析，GitHub 改版
   頁面結構時會失效。程式已做防禦——爬蟲失敗只會少掉該來源，
   日報仍會由 Search API 的新星專案照常產出。
2. **排程延遲**：GitHub 官方文件註明 scheduled workflow 在尖峰時段
   可能延遲數分鐘至數十分鐘，07:00 的報可能 07:20 才到。
3. **Star 數不等於品質**：選稿以 star 動能為代理指標，會漏掉低調的好專案、
   也可能收進行銷驅動的專案。主編 prompt 已要求 Claude 對成熟度
   做誠實評估，但最終判斷仍在讀者。
4. **README 摘錄上限 2,500 字元**：控制成本的取捨，摘要深度受此限制。

## 資料出處

- GitHub Search API 語法與速率限制：docs.github.com/en/rest/search/search
- GitHub Actions 排程與計費：docs.github.com/en/actions
- Anthropic Messages API：docs.claude.com/en/api/messages
- Gmail 應用程式密碼：support.google.com/accounts/answer/185833
