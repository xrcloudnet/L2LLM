# L2LLM ä¾èµ–å®‰è£…æ¸…å•

æ›´æ–°æ—¶é—´ï¼š2026-06-02

## 1. Python è¿è¡ŒçŽ¯å¢ƒ

å»ºè®®ä½¿ç”¨ Python 3.12ã€‚

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

å¦‚æžœä½¿ç”¨ Codex å†…ç½®è¿è¡Œæ—¶ï¼Œå¯ç›´æŽ¥ç”¨å½“å‰é¡¹ç›®è„šæœ¬ä¸­çš„è§£é‡Šå™¨ï¼š

```powershell
& "C:\Users\windows11\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m pip install -r requirements.txt
```

## 2. Python åŒ…è¯´æ˜Ž

| ä¾èµ– | ç”¨é€” | æ˜¯å¦å¿…éœ€ |
|---|---|---|
| `fastapi` | åŽç«¯ API æœåŠ¡ | å¿…éœ€ |
| `uvicorn` | FastAPI ASGI æœåŠ¡å™¨ | å¿…éœ€ |
| `pandas` | Kçº¿ã€æŒ‡æ ‡ã€è¡Œæƒ…æ•°æ®å¤„ç† | å¿…éœ€ |
| `numpy` | æ•°å€¼è®¡ç®—è¾…åŠ© | å¿…éœ€ |
| `httpx` | iFinDã€Yahooã€Twelve Dataã€OpenAI/Gemini HTTP è¯·æ±‚ | å¿…éœ€ |
| `akshare` | A è‚¡å¤‡ç”¨è¡Œæƒ…ã€ç›˜å£ã€èµ„é‡‘æµ | å¿…éœ€ |
| `SQLAlchemy` | SQLite æœ¬åœ°åŽ†å²åº“ ORM | å¿…éœ€ |
| `duckdb` | DuckDB æœ¬åœ°åˆ†æžåº“ | æŽ¨èå¯ç”¨ |
| `redis` | Redis/Memurai é«˜é¢‘ç¼“å­˜å®¢æˆ·ç«¯ | æŽ¨èå¯ç”¨ |
| `moomoo-api` | Moomoo OpenD ç¾Žè‚¡/æ¸¯è‚¡ä¸»æ•°æ®æº SDK | ç¾Žè‚¡/æ¸¯è‚¡æŽ¨è |
| `matplotlib` | åŽç»­ç ”ç©¶/å›žæµ‹å›¾è¡¨è¾“å‡º | å¯é€‰ |
| `backtrader` | åŽç»­å›žæµ‹æ¨¡å— | å¯é€‰ |

## 3. æœ¬åœ°æœåŠ¡ä¾èµ–

### Redis / Memurai

ç”¨äºŽé«˜é¢‘ç¼“å­˜ã€iFinD Push quote/tick å¿«ç…§ã€ç¬¬ä¸‰æ–¹ AI ä½Žé¢‘ç¼“å­˜ã€‚

å½“å‰é¡¹ç›®é»˜è®¤è¿žæŽ¥ï¼š

```powershell
$env:USE_REDIS="1"
$env:REDIS_HOST="127.0.0.1"
$env:REDIS_PORT="6379"
$env:REDIS_DB="0"
```

Windows ä¸Šå¯ä½¿ç”¨ Memuraiï¼›å¦‚æžœä½¿ç”¨ Docker Redisï¼š

```powershell
docker compose -f docker-compose.storage.yml up -d
```

### DuckDB

ä¸éœ€è¦å•ç‹¬å®‰è£…æœåŠ¡ï¼Œåªéœ€è¦ Python åŒ… `duckdb`ã€‚æ•°æ®åº“æ–‡ä»¶ï¼š

```text
data/l2llm.duckdb
```

### SQLite

Python æ ‡å‡†åº“è‡ªå¸¦ï¼Œä¸éœ€è¦é¢å¤–å®‰è£…ã€‚æ•°æ®åº“æ–‡ä»¶ï¼š

```text
data/l2llm.db
```

## 4. å¤–éƒ¨æ•°æ®æºä¾èµ–

### A è‚¡ï¼šiFinD

éœ€è¦é…ç½® tokenï¼š

```powershell
$env:USE_IFIND="1"
$env:IFIND_ACCESS_TOKEN="<your-ifind-access-token>"
# æˆ–é…ç½® refresh tokenï¼Œç”±åŽç«¯èŽ·å– access token
$env:IFIND_REFRESH_TOKEN="<your-ifind-refresh-token>"
```

iFinD Push ç¼“å­˜å±‚é»˜è®¤å¯ç”¨ï¼š

```powershell
$env:USE_IFIND_PUSH="1"
$env:IFIND_PUSH_TTL="6"
$env:IFIND_PUSH_STALE_SECONDS="8"
```

å½“å‰çš„ iFinD tick æ˜¯ HTTP å®žæ—¶æŠ¥ä»·æ¡¥æŽ¥é‡‡æ ·ï¼š

```powershell
python scripts\ifind_push_bridge.py 688981 600519 --interval 1
```

### ç¾Žè‚¡/æ¸¯è‚¡ï¼šMoomoo OpenD

éœ€è¦æœ¬æœºå®‰è£…å¹¶å¯åŠ¨ Moomoo OpenDï¼Œé»˜è®¤è¿žæŽ¥ï¼š

```powershell
$env:USE_MOOMOO="1"
$env:MOOMOO_HOST="127.0.0.1"
$env:MOOMOO_PORT="11111"
```

Python SDK ç”± `moomoo-api` æä¾›ã€‚

æ³¨æ„ï¼š`moomoo-api` åœ¨å¯¼å…¥æ—¶ä¼šå†™å…¥æœ¬æœºæ—¥å¿—ç›®å½•ï¼Œä¾‹å¦‚ï¼š

```text
C:\Users\windows11\AppData\Roaming\com.moomoo.OpenD\Log
```

å¦‚æžœåœ¨å—é™æ²™ç®±æˆ–æƒé™ä¸è¶³çš„ç»ˆç«¯é‡Œçœ‹åˆ° `PermissionError`ï¼Œé€šå¸¸ä¸æ˜¯ä¾èµ–ç¼ºå¤±ï¼Œè€Œæ˜¯æ—¥å¿—ç›®å½•ä¸å¯å†™ã€‚è¯·ç”¨æ­£å¸¸ç”¨æˆ· PowerShell å¯åŠ¨åŽç«¯ï¼Œæˆ–ç¡®è®¤è¯¥ç›®å½•å¯å†™ã€‚

### Twelve Data

ä½œä¸ºç¾Žè‚¡/æ¸¯è‚¡å¤‡ç”¨æ•°æ®æºï¼š

```powershell
$env:TWELVE_DATA_API_KEY="<your-twelve-data-api-key>"
```

## 5. ç¬¬ä¸‰æ–¹ AI ä¾èµ–

### OpenAI

```powershell
$env:AI_PROVIDER="openai"
$env:USE_OPENAI="1"
$env:OPENAI_API_KEY="ä½ çš„ OpenAI Key"
$env:OPENAI_MODEL="gpt-5-mini"
```

### Gemini

```powershell
$env:AI_PROVIDER="gemini"
$env:GEMINI_API_KEY="ä½ çš„ Gemini Key"
$env:GEMINI_MODEL="gemini-2.5-flash"
```

ä½Žé¢‘å’Œè¶…æ—¶å‚æ•°ï¼š

```powershell
$env:THIRD_PARTY_AI_TIMEOUT="20"
$env:THIRD_PARTY_AI_MIN_INTERVAL="300"
$env:OPENAI_MAX_OUTPUT_TOKENS="1000"
$env:OPENAI_REASONING_EFFORT="minimal"
```

## 6. å‰ç«¯ä¾èµ–

å½“å‰å‰ç«¯æ˜¯åŽŸç”Ÿ HTML/CSS/JavaScript + Canvasï¼Œä¸éœ€è¦ `npm install`ã€‚

`package.json` åªæä¾›å¯åŠ¨è„šæœ¬ï¼š

```powershell
npm start
```

ç­‰ä»·äºŽï¼š

```powershell
.\run_fastapi.ps1
```

## 7. æŽ¨èå¯åŠ¨é¡ºåº

1. å¯åŠ¨ Redis/Memuraiã€‚
2. å¯åŠ¨ Moomoo OpenDï¼Œå¦‚æžœéœ€è¦ç¾Žè‚¡/æ¸¯è‚¡ä¸»è¡Œæƒ…æºã€‚
3. å¯åŠ¨åŽç«¯ï¼š

```powershell
.\run_fastapi.ps1
```

4. éœ€è¦ iFinD ç§’çº§é‡‡æ · tick æ—¶ï¼Œå¦å¼€ä¸€ä¸ª PowerShellï¼š

```powershell
python scripts\ifind_push_bridge.py 688981 600519 --interval 1
```

5. æ‰“å¼€ï¼š

```text
http://localhost:5177
```

## 8. å¿«é€ŸéªŒè¯

```powershell
python backend\test.py
python scripts\read_duckdb.py counts
python scripts\openai_probe.py
```

å¥åº·æ£€æŸ¥ï¼š

```text
http://127.0.0.1:5177/api/health
```

