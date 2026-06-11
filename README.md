# L2LLM è‚¡ç¥¨ AI è¡Œæƒ…

L2LLM æ˜¯ä¸€ä¸ªæœ¬åœ°è‚¡ç¥¨è¡Œæƒ…ç ”ç©¶é¢æ¿ï¼Œæ”¯æŒä¸­å›½ A è‚¡ã€ç¾Žè‚¡ã€æ¸¯è‚¡ã€‚ç³»ç»ŸåŒ…å«æŠ¥ä»·ã€ç›˜å£ã€Kçº¿ã€ç§’çº§ tickã€ä¸»å›¾ MACDã€ç§’çº§ MACDã€DDE èµ„é‡‘æµã€æœ¬åœ° AI åˆ¤æ–­ã€ç¬¬ä¸‰æ–¹ OpenAI/Gemini ç‹¬ç«‹åˆ†æžï¼Œä»¥åŠä¸‰å¸‚åœºä»“ä½ç®¡ç†ã€‚

> è¾“å‡ºä»…ç”¨äºŽè¡Œæƒ…ç ”ç©¶å’Œè¾…åŠ©å†³ç­–ï¼Œä¸æž„æˆæŠ•èµ„å»ºè®®ã€‚

## è¿è¡Œ

```powershell
.\run_fastapi.ps1
```

æ‰“å¼€ï¼š

```text
http://localhost:5177
```

## æŠ€æœ¯æ ˆ

- åŽç«¯ï¼šFastAPI
- æ•°æ®å¤„ç†ï¼šPandas
- A è‚¡æ•°æ®æºï¼šiFinD HTTP API -> AKShare/Eastmoney/Sina fallback
- ç¾Žè‚¡/æ¸¯è‚¡æ•°æ®æºï¼šMoomoo OpenD -> Yahoo Finance -> Twelve Data fallback
- æœ¬åœ° AIï¼šè§„åˆ™å¼•æ“Žï¼Œè¦†ç›–ç›˜å£ã€Kçº¿ã€DDEã€MACDã€çŸ­çº¿è¡Œä¸ºä¿¡å·
- ç¬¬ä¸‰æ–¹ AIï¼šOpenAI/ChatGPT æˆ– Geminiï¼ŒåŽå°ä½Žé¢‘ç‹¬ç«‹åˆ†æžï¼Œä¸é˜»å¡žæœ¬åœ° AI
- é«˜é¢‘ç¼“å­˜ï¼šRedis/Memuraiï¼Œå¤±è´¥æ—¶å›žé€€è¿›ç¨‹å†…ç¼“å­˜
- æœ¬åœ°æ•°æ®åº“ï¼šDuckDB + SQLite
- å‰ç«¯ï¼šåŽŸç”Ÿ HTML/CSS/JavaScript + Canvas

## å½“å‰èƒ½åŠ›

- ä¸»å›¾ Kçº¿æ”¯æŒ `1m/2m/5m/15m/30m/60m/1d/1wk/1mo/3mo/6mo`ã€‚
- range æ”¯æŒ `1d/5d/1mo/3mo/6mo/ytd/1y/3y/5y/10y/all`ã€‚
- ç§’çº§å®žæ—¶è¡Œæƒ…é€šè¿‡ `/api/realtime` æ¯ç§’åˆ·æ–°ã€‚
- ç§’çº§ MACD ç‹¬ç«‹ä½¿ç”¨å½“æ—¥å®žæ—¶ tick èšåˆ 3 ç§’ barï¼Œä¸å—ä¸»å›¾ range/interval å½±å“ã€‚
- ä¸»å›¾ MACD ä½¿ç”¨å½“å‰ä¸»å›¾ candlesï¼Œä¾‹å¦‚ `1Y+1d` æ˜¯æ—¥K MACDï¼Œ`1D+1m` æ˜¯1åˆ†é’Ÿ MACDï¼Œ`10Y+1wk` æ˜¯å‘¨K MACDã€‚
- ç¬¬ä¸‰æ–¹ AI é¢æ¿æœ‰â€œå¯åŠ¨/å…³é—­â€å•é€‰æŒ‰é’®ã€‚å…³é—­åŽä¸è¯·æ±‚ OpenAI/Geminiï¼Œåªæ˜¾ç¤ºå·²æœ‰ç¼“å­˜ï¼›æ— ç¼“å­˜åˆ™ä¸ºç©ºã€‚
- ç¬¬ä¸‰æ–¹ AI é»˜è®¤ 300 ç§’ä½Žé¢‘ç¼“å­˜ï¼ŒåŽå°è¯·æ±‚ï¼Œä¸æ‹–æ…¢æœ¬åœ° AIã€‚
- æ¶¨è·Œé¢œè‰²ç»Ÿä¸€ä¸ºä¸Šæ¶¨çº¢è‰²ã€ä¸‹è·Œç»¿è‰²ã€‚

## API

| è·¯å¾„ | è¯´æ˜Ž |
|---|---|
| `GET /api/health` | å¥åº·æ£€æŸ¥ï¼Œè¿”å›ž Redisã€DuckDBã€AI é…ç½®çŠ¶æ€ |
| `GET /api/market` | è¡Œæƒ…ã€Kçº¿ã€ç›˜å£ã€èµ„é‡‘æµ |
| `GET /api/realtime` | ç§’çº§å®žæ—¶ quote |
| `POST /api/analyze` | æœ¬åœ° AI + ç¬¬ä¸‰æ–¹ AI ç‹¬ç«‹åˆ†æž |
| `GET /api/portfolio` | ä»“ä½å¿«ç…§ |
| `POST /api/portfolio/trades` | ä¿å­˜äº¤æ˜“ |
| `GET /api/history/candles` | æŸ¥è¯¢æœ¬åœ° Kçº¿åŽ†å² |
| `GET /api/history/analysis` | æŸ¥è¯¢ AI åˆ¤æ–­åŽ†å² |

## OpenAI / Gemini

OpenAIï¼š

```powershell
$env:AI_PROVIDER="openai"
$env:OPENAI_API_KEY="ä½ çš„å¯†é’¥"
$env:USE_OPENAI="1"
$env:OPENAI_MODEL="gpt-5-mini"
.\run_fastapi.ps1
```

Geminiï¼š

```powershell
$env:AI_PROVIDER="gemini"
$env:GEMINI_API_KEY="ä½ çš„å¯†é’¥"
$env:GEMINI_MODEL="gemini-2.5-flash"
.\run_fastapi.ps1
```

å¯è°ƒå‚æ•°ï¼š

```powershell
$env:THIRD_PARTY_AI_TIMEOUT="20"
$env:THIRD_PARTY_AI_MIN_INTERVAL="300"
$env:OPENAI_MAX_OUTPUT_TOKENS="1000"
$env:OPENAI_REASONING_EFFORT="minimal"
```

## Redis + DuckDB

```powershell
docker compose -f docker-compose.storage.yml up -d

$env:USE_REDIS="1"
$env:REDIS_HOST="127.0.0.1"
$env:REDIS_PORT="6379"
$env:REDIS_DB="0"
$env:USE_DUCKDB="1"

.\run_fastapi.ps1
```

- Redisï¼šçŸ­ TTL é«˜é¢‘ç¼“å­˜ï¼ŒåŒ…æ‹¬è¡Œæƒ…ã€ç›˜å£ã€iFinD tokenã€ç¬¬ä¸‰æ–¹ AI ç»“æžœã€‚
- DuckDBï¼šKçº¿åŽ†å²ä¸Ž AI åˆ¤æ–­åŽ†å²çš„æœ¬åœ°åˆ†æžåž‹å­˜å‚¨ï¼Œæ–‡ä»¶ä½äºŽ `data/l2llm.duckdb`ã€‚
- SQLiteï¼šè½»é‡æœ¬åœ°æ•°æ®åº“ï¼Œæ–‡ä»¶ä½äºŽ `data/l2llm.db`ï¼Œä¹Ÿä½œä¸ºå¤–éƒ¨æ•°æ®å¤±è´¥æ—¶çš„æœ¬åœ°åŽ†å²å…œåº•ã€‚

## æ–‡æ¡£

å®Œæ•´å‡½æ•°æ¸…å•ã€åŠŸèƒ½è¯´æ˜Žå’Œ UML å›¾è§ï¼š

```text
docs/project_functions_uml.md
```

ä¾èµ–å®‰è£…ä¸Žæœ¬åœ°æœåŠ¡å‡†å¤‡è§ï¼š

```text
docs/dependencies.md
```

## OpenAI è¿žé€šæ€§è¯Šæ–­

ä¸æ‰“å°å¯†é’¥ï¼Œåªæµ‹è¯•æœ€å° Responses API è¯·æ±‚ï¼š

```powershell
python scripts\openai_probe.py
```

## iFinD Push Cache

A è‚¡å®žæ—¶ quote çŽ°åœ¨æ”¯æŒæŽ¨é€ç¼“å­˜ä¼˜å…ˆçº§ï¼š

```text
iFinD Push Redis snapshot -> iFinD HTTP real_time_quotation -> Sina fallback
```

å¯åŠ¨åŽç«¯æ—¶é»˜è®¤å¯ç”¨ï¼š

```powershell
$env:USE_IFIND_PUSH="1"
$env:IFIND_PUSH_TTL="6"
$env:IFIND_PUSH_STALE_SECONDS="8"
.\run_fastapi.ps1
```

å½“å‰å…ˆæä¾› HTTP æ¡¥æŽ¥è„šæœ¬ï¼ŒæŠŠ iFinD å®žæ—¶æŠ¥ä»·å†™å…¥ Redis æŽ¨é€ç¼“å­˜ï¼š

```powershell
python scripts\ifind_push_bridge.py 688981 600519 --interval 1 --concurrency 5
```

æ­£å¼ iFinD æŽ¨é€ SDK æŽ¥å…¥æ—¶ï¼Œåªéœ€è¦æŒ‰åŒæ ·æ ¼å¼å†™å…¥ Redis keyï¼š

```text
l2llm:ifind:push:quote:SH688981
```

