import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const publicDir = join(__dirname, "public");
const PORT = Number(process.env.PORT || 5177);

const mime = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml"
};

const cache = new Map();

function json(res, status, payload) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store"
  });
  res.end(JSON.stringify(payload));
}

async function cachedFetchJson(key, ttlMs, url) {
  const now = Date.now();
  const hit = cache.get(key);
  if (hit && now - hit.time < ttlMs) return hit.data;

  const response = await fetch(url, {
    headers: {
      "accept": "application/json",
      "user-agent": "L2LLM/0.1 market research dashboard"
    }
  });

  if (!response.ok) {
    throw new Error(`Market data provider returned ${response.status}`);
  }

  const data = await response.json();
  cache.set(key, { time: now, data });
  return data;
}

async function cachedFetchText(key, ttlMs, url, options = {}) {
  const now = Date.now();
  const hit = cache.get(key);
  if (hit && now - hit.time < ttlMs) return hit.data;

  const response = await fetch(url, {
    headers: {
      "accept": "*/*",
      "user-agent": "L2LLM/0.1 market research dashboard",
      ...(options.headers || {})
    }
  });

  if (!response.ok) {
    throw new Error(`Market data provider returned ${response.status}`);
  }

  const charset = response.headers.get("content-type")?.match(/charset=([^;]+)/i)?.[1] || "utf-8";
  const decoder = new TextDecoder(charset.toLowerCase() === "gb18030" ? "gb18030" : "utf-8");
  const data = decoder.decode(await response.arrayBuffer());
  cache.set(key, { time: now, data });
  return data;
}

function asNumber(value) {
  return Number.isFinite(Number(value)) ? Number(value) : null;
}

function normalizeAShareSymbol(input) {
  const raw = input.trim().toUpperCase();
  const compact = raw.replace(/\s+/g, "");
  let code = null;
  let exchange = null;

  const prefixed = compact.match(/^(SH|SZ)(\d{6})$/);
  const suffixed = compact.match(/^(\d{6})\.(SH|SZ|SS|SZ)$/);
  const plain = compact.match(/^(\d{6})$/);

  if (prefixed) {
    exchange = prefixed[1].toLowerCase();
    code = prefixed[2];
  } else if (suffixed) {
    code = suffixed[1];
    exchange = suffixed[2] === "SS" ? "sh" : suffixed[2].toLowerCase();
  } else if (plain) {
    code = plain[1];
    exchange = code.startsWith("6") || code.startsWith("9") ? "sh" : "sz";
  }

  if (!code || !exchange) return null;

  return {
    code,
    exchange,
    sina: `${exchange}${code}`,
    eastmoneySecid: `${exchange === "sh" ? 1 : 0}.${code}`,
    display: `${exchange.toUpperCase()}${code}`
  };
}

function eastmoneyKlt(interval) {
  return {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
    "1d": "101"
  }[interval] || "1";
}

function rangeWindowMs(range) {
  return {
    "1d": 1,
    "5d": 7,
    "1mo": 35,
    "3mo": 100,
    "6mo": 190,
    "1y": 380
  }[range] * 24 * 60 * 60 * 1000;
}

function parseYahooChart(raw, symbol) {
  const result = raw?.chart?.result?.[0];
  const error = raw?.chart?.error;
  if (!result || error) {
    throw new Error(error?.description || `No market data for ${symbol}`);
  }

  const meta = result.meta || {};
  const timestamps = result.timestamp || [];
  const quote = result.indicators?.quote?.[0] || {};
  const candles = timestamps.map((time, index) => ({
    time: time * 1000,
    open: asNumber(quote.open?.[index]),
    high: asNumber(quote.high?.[index]),
    low: asNumber(quote.low?.[index]),
    close: asNumber(quote.close?.[index]),
    volume: asNumber(quote.volume?.[index])
  })).filter((candle) => (
    candle.open !== null &&
    candle.high !== null &&
    candle.low !== null &&
    candle.close !== null
  ));

  const previousClose = asNumber(meta.previousClose || meta.chartPreviousClose);
  const price = asNumber(meta.regularMarketPrice) ?? candles.at(-1)?.close ?? null;
  const change = price !== null && previousClose ? price - previousClose : null;
  const changePercent = change !== null && previousClose ? (change / previousClose) * 100 : null;
  const dayHigh = candles.length ? Math.max(...candles.map((c) => c.high)) : null;
  const dayLow = candles.length ? Math.min(...candles.map((c) => c.low)) : null;

  return {
    symbol: meta.symbol || symbol.toUpperCase(),
    exchange: meta.fullExchangeName || meta.exchangeName || "Unknown",
    currency: meta.currency || "",
    marketState: meta.marketState || "UNKNOWN",
    provider: "Yahoo Finance chart API",
    delayed: true,
    updatedAt: new Date().toISOString(),
    quote: {
      price,
      previousClose,
      open: asNumber(meta.regularMarketDayOpen) ?? candles[0]?.open ?? null,
      dayHigh: asNumber(meta.regularMarketDayHigh) ?? dayHigh,
      dayLow: asNumber(meta.regularMarketDayLow) ?? dayLow,
      volume: asNumber(meta.regularMarketVolume),
      change,
      changePercent
    },
    candles
  };
}

function parseSinaQuote(text, symbolInfo) {
  const match = text.match(/="([^"]*)"/);
  if (!match || !match[1]) {
    throw new Error(`No A-share quote data for ${symbolInfo.display}`);
  }

  const fields = match[1].split(",");
  if (fields.length < 33 || !fields[0]) {
    throw new Error(`No A-share quote data for ${symbolInfo.display}`);
  }

  const numberAt = (index) => asNumber(fields[index]);
  const bids = [0, 1, 2, 3, 4].map((level) => ({
    size: numberAt(10 + level * 2) || 0,
    price: numberAt(11 + level * 2) || 0
  })).filter((row) => row.price > 0);
  const asks = [0, 1, 2, 3, 4].map((level) => ({
    size: numberAt(20 + level * 2) || 0,
    price: numberAt(21 + level * 2) || 0
  })).filter((row) => row.price > 0);
  const bidTotal = bids.reduce((sum, row) => sum + row.size, 0);
  const askTotal = asks.reduce((sum, row) => sum + row.size, 0);
  const price = numberAt(3);
  const previousClose = numberAt(2);
  const change = price !== null && previousClose !== null ? price - previousClose : null;
  const changePercent = change !== null && previousClose ? change / previousClose * 100 : null;

  return {
    name: fields[0],
    quote: {
      price,
      previousClose,
      open: numberAt(1),
      dayHigh: numberAt(4),
      dayLow: numberAt(5),
      volume: numberAt(8),
      amount: numberAt(9),
      change,
      changePercent
    },
    orderBook: {
      provider: "Sina Finance 5-level quote",
      note: "A股盘口来自新浪五档买卖盘；不同交易时段和免费源可能存在延迟或快照缺口。",
      bids,
      asks,
      imbalance: (bidTotal - askTotal) / Math.max(bidTotal + askTotal, 1)
    },
    quoteTime: `${fields[30] || ""} ${fields[31] || ""}`.trim()
  };
}

function parseEastmoneyKlines(raw, range) {
  const klines = raw?.data?.klines || [];
  const cutoff = Date.now() - rangeWindowMs(range);
  const candles = klines.map((line) => {
    const parts = line.split(",");
    return {
      time: Date.parse(`${parts[0].replace(" ", "T")}+08:00`),
      open: asNumber(parts[1]),
      close: asNumber(parts[2]),
      high: asNumber(parts[3]),
      low: asNumber(parts[4]),
      volume: asNumber(parts[5]) !== null ? asNumber(parts[5]) * 100 : null,
      amount: asNumber(parts[6])
    };
  }).filter((candle) => (
    Number.isFinite(candle.time) &&
    candle.open !== null &&
    candle.high !== null &&
    candle.low !== null &&
    candle.close !== null &&
    (range === "1d" || candle.time >= cutoff)
  ));

  return range === "1d" ? candles.slice(-320) : candles.slice(-900);
}

async function fetchAShareMarket(symbolInfo, range, interval) {
  const quoteUrl = `https://hq.sinajs.cn/list=${symbolInfo.sina}`;
  const klineUrl = `https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=${symbolInfo.eastmoneySecid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=${eastmoneyKlt(interval)}&fqt=1&beg=0&end=20500101`;
  const [quoteText, klineRaw] = await Promise.all([
    cachedFetchText(`sina:${symbolInfo.sina}`, 3000, quoteUrl, {
      headers: { referer: "https://finance.sina.com.cn" }
    }),
    cachedFetchJson(`eastmoney:${symbolInfo.eastmoneySecid}:${range}:${interval}`, 6000, klineUrl)
  ]);

  const quoteData = parseSinaQuote(quoteText, symbolInfo);
  const candles = parseEastmoneyKlines(klineRaw, range);
  const candleHigh = candles.length ? Math.max(...candles.map((c) => c.high)) : null;
  const candleLow = candles.length ? Math.min(...candles.map((c) => c.low)) : null;

  return {
    symbol: symbolInfo.display,
    name: quoteData.name,
    exchange: symbolInfo.exchange === "sh" ? "Shanghai Stock Exchange" : "Shenzhen Stock Exchange",
    currency: "CNY",
    marketState: "A股",
    provider: "Sina Finance quote + Eastmoney K-line",
    delayed: true,
    updatedAt: new Date().toISOString(),
    quote: {
      ...quoteData.quote,
      dayHigh: quoteData.quote.dayHigh ?? candleHigh,
      dayLow: quoteData.quote.dayLow ?? candleLow
    },
    candles,
    orderBook: quoteData.orderBook,
    quoteTime: quoteData.quoteTime
  };
}

function synthesizeOrderBook(price, candles) {
  if (!price) return { provider: "synthetic", bids: [], asks: [], imbalance: 0 };

  const recent = candles.slice(-30);
  const averageRange = recent.reduce((sum, c) => sum + Math.max(c.high - c.low, 0), 0) / Math.max(recent.length, 1);
  const tick = Math.max(price * 0.0003, averageRange * 0.08, 0.01);
  const momentum = recent.length > 4 ? (recent.at(-1).close - recent.at(-5).close) / recent.at(-5).close : 0;
  const bidBias = Math.max(-0.35, Math.min(0.35, momentum * 12));

  const bids = Array.from({ length: 8 }, (_, i) => {
    const level = i + 1;
    return {
      price: price - tick * level,
      size: Math.round((900 + Math.random() * 2200) * (1 + bidBias) / level ** 0.25)
    };
  });

  const asks = Array.from({ length: 8 }, (_, i) => {
    const level = i + 1;
    return {
      price: price + tick * level,
      size: Math.round((900 + Math.random() * 2200) * (1 - bidBias) / level ** 0.25)
    };
  });

  const bidTotal = bids.reduce((sum, row) => sum + row.size, 0);
  const askTotal = asks.reduce((sum, row) => sum + row.size, 0);

  return {
    provider: "synthetic-depth",
    note: "免费股票源通常不含实时Level2盘口；此处用价格波动估算展示，生产环境请替换为券商/交易所深度接口。",
    bids,
    asks,
    imbalance: (bidTotal - askTotal) / Math.max(bidTotal + askTotal, 1)
  };
}

function sma(values, size) {
  if (values.length < size) return null;
  return values.slice(-size).reduce((sum, value) => sum + value, 0) / size;
}

function rsi(closes, period = 14) {
  if (closes.length <= period) return null;
  let gains = 0;
  let losses = 0;
  for (let i = closes.length - period; i < closes.length; i += 1) {
    const delta = closes[i] - closes[i - 1];
    if (delta >= 0) gains += delta;
    else losses -= delta;
  }
  if (losses === 0) return 100;
  const rs = gains / losses;
  return 100 - 100 / (1 + rs);
}

function buildHeuristicAnalysis(symbol, quote, candles, orderBook) {
  const closes = candles.map((c) => c.close).filter(Number.isFinite);
  const volumes = candles.map((c) => c.volume || 0);
  const last = closes.at(-1);
  const ma20 = sma(closes, 20);
  const ma60 = sma(closes, 60);
  const rsi14 = rsi(closes, 14);
  const recentVolume = volumes.slice(-5).reduce((a, b) => a + b, 0) / Math.max(volumes.slice(-5).length, 1);
  const baseVolume = volumes.slice(-40, -5).reduce((a, b) => a + b, 0) / Math.max(volumes.slice(-40, -5).length, 1);
  const volumeRatio = baseVolume ? recentVolume / baseVolume : 1;
  const high20 = Math.max(...candles.slice(-20).map((c) => c.high));
  const low20 = Math.min(...candles.slice(-20).map((c) => c.low));

  let score = 0;
  const reasons = [];
  const risks = [];

  if (last && ma20) {
    const above = (last - ma20) / ma20;
    score += above > 0 ? 1 : -1;
    reasons.push(`价格${above > 0 ? "站上" : "跌破"}20周期均线 ${Math.abs(above * 100).toFixed(2)}%`);
  }
  if (ma20 && ma60) {
    score += ma20 > ma60 ? 1 : -1;
    reasons.push(`20周期均线${ma20 > ma60 ? "高于" : "低于"}60周期均线`);
  }
  if (rsi14 !== null) {
    if (rsi14 > 70) {
      score -= 0.5;
      risks.push("RSI处于偏热区间，追高回撤风险上升");
    } else if (rsi14 < 30) {
      score += 0.5;
      risks.push("RSI处于偏冷区间，可能有反弹但也代表弱势未修复");
    } else {
      reasons.push(`RSI ${rsi14.toFixed(1)}，动能未进入极端区`);
    }
  }
  if (volumeRatio > 1.4) {
    score += quote.changePercent >= 0 ? 0.8 : -0.8;
    reasons.push(`近5根成交量约为基准的 ${volumeRatio.toFixed(2)} 倍`);
  }
  if (orderBook?.imbalance) {
    score += orderBook.imbalance * 1.5;
    reasons.push(`盘口买卖量差 ${(orderBook.imbalance * 100).toFixed(1)}%`);
  }

  const direction = score > 1.1 ? "偏多" : score < -1.1 ? "偏空" : "震荡";
  const confidence = Math.max(35, Math.min(88, 48 + Math.abs(score) * 13 + Math.min(volumeRatio, 2) * 4));
  const action = direction === "偏多"
    ? "关注回踩均线后的承接，避免直接追涨。"
    : direction === "偏空"
      ? "关注反抽压力和止损纪律，弱势下不急于抄底。"
      : "等待放量突破或跌破区间后再提高仓位。";

  if (last && high20 && low20) {
    risks.push(`近20周期压力约 ${high20.toFixed(2)}，支撑约 ${low20.toFixed(2)}`);
  }

  return {
    engine: "heuristic",
    symbol,
    direction,
    confidence: Math.round(confidence),
    score: Number(score.toFixed(2)),
    summary: `${symbol} 当前判断为${direction}，置信度 ${Math.round(confidence)}%。${action}`,
    metrics: {
      ma20,
      ma60,
      rsi14,
      volumeRatio,
      support: Number.isFinite(low20) ? low20 : null,
      resistance: Number.isFinite(high20) ? high20 : null
    },
    reasons: reasons.slice(0, 5),
    risks: risks.slice(0, 4),
    disclaimer: "仅用于行情研究和策略辅助，不构成投资建议。"
  };
}

async function modelAnalysis(payload, heuristic) {
  if (!process.env.OPENAI_API_KEY) {
    return {
      ...heuristic,
      openaiStatus: "disabled",
      openaiReason: "OPENAI_API_KEY is not visible to the running Node process."
    };
  }
  if (process.env.USE_OPENAI !== "1") {
    return {
      ...heuristic,
      openaiStatus: "disabled",
      openaiReason: "USE_OPENAI must be set to 1."
    };
  }

  const compact = {
    symbol: payload.symbol,
    quote: payload.quote,
    orderBook: {
      imbalance: payload.orderBook?.imbalance,
      provider: payload.orderBook?.provider
    },
    recentCandles: payload.candles.slice(-80),
    heuristic
  };

  const response = await fetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      "authorization": `Bearer ${process.env.OPENAI_API_KEY}`,
      "content-type": "application/json"
    },
    body: JSON.stringify({
      model: process.env.OPENAI_MODEL || "gpt-5-mini",
      input: [
        {
          role: "system",
          content: "你是股票行情研究助手。基于报价、盘口和K线指标给出短线行情解读。必须强调不构成投资建议。输出JSON。"
        },
        {
          role: "user",
          content: `请分析以下行情数据，返回字段：direction, confidence, summary, reasons, risks, metrics。\n${JSON.stringify(compact)}`
        }
      ],
      text: {
        format: {
          type: "json_object"
        }
      }
    })
  });

  const requestId = response.headers.get("x-request-id");
  if (!response.ok) {
    let errorMessage = `OpenAI API returned ${response.status}`;
    try {
      const errorBody = await response.json();
      errorMessage = errorBody?.error?.message || errorMessage;
    } catch {
      errorMessage = await response.text().catch(() => errorMessage);
    }
    console.error("[OpenAI]", errorMessage, requestId ? `request_id=${requestId}` : "");
    return {
      ...heuristic,
      openaiStatus: "failed",
      openaiReason: errorMessage,
      openaiRequestId: requestId
    };
  }
  const data = await response.json();
  const text = data.output_text || data.output?.flatMap((item) => item.content || []).find((part) => part.type === "output_text")?.text;
  if (!text) {
    return {
      ...heuristic,
      openaiStatus: "failed",
      openaiReason: "OpenAI response did not contain output_text.",
      openaiRequestId: requestId
    };
  }

  try {
    return {
      ...heuristic,
      ...JSON.parse(text),
      engine: "openai",
      openaiStatus: "ok",
      openaiRequestId: requestId,
      disclaimer: "仅用于行情研究和策略辅助，不构成投资建议。"
    };
  } catch {
    return {
      ...heuristic,
      openaiStatus: "failed",
      openaiReason: "OpenAI output was not valid JSON.",
      openaiRequestId: requestId
    };
  }
}

function handleHealth(res) {
  json(res, 200, {
    ok: true,
    openai: {
      apiKeyVisible: Boolean(process.env.OPENAI_API_KEY),
      apiKeyPrefix: process.env.OPENAI_API_KEY ? `${process.env.OPENAI_API_KEY.slice(0, 7)}...` : null,
      useOpenAI: process.env.USE_OPENAI || null,
      enabled: Boolean(process.env.OPENAI_API_KEY) && process.env.USE_OPENAI === "1",
      model: process.env.OPENAI_MODEL || "gpt-5-mini"
    }
  });
}

async function handleMarket(req, res, url) {
  const symbol = (url.searchParams.get("symbol") || "AAPL").trim().toUpperCase();
  const range = url.searchParams.get("range") || "1d";
  const interval = url.searchParams.get("interval") || "1m";
  const allowedRanges = new Set(["1d", "5d", "1mo", "3mo", "6mo", "1y"]);
  const allowedIntervals = new Set(["1m", "2m", "5m", "15m", "30m", "60m", "1d"]);

  if (!/^[A-Z0-9.\-=^]{1,20}$/.test(symbol)) {
    json(res, 400, { error: "Invalid symbol" });
    return;
  }
  if (!allowedRanges.has(range) || !allowedIntervals.has(interval)) {
    json(res, 400, { error: "Invalid range or interval" });
    return;
  }

  const aShare = normalizeAShareSymbol(symbol);
  if (aShare) {
    const market = await fetchAShareMarket(aShare, range, interval);
    json(res, 200, market);
    return;
  }

  const providerUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=${interval}&range=${range}&includePrePost=true`;
  const raw = await cachedFetchJson(`${symbol}:${range}:${interval}`, 8000, providerUrl);
  const market = parseYahooChart(raw, symbol);
  market.orderBook = synthesizeOrderBook(market.quote.price, market.candles);
  json(res, 200, market);
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

async function handleAnalyze(req, res) {
  const body = JSON.parse(await readBody(req) || "{}");
  const heuristic = buildHeuristicAnalysis(body.symbol || "UNKNOWN", body.quote || {}, body.candles || [], body.orderBook || {});
  const analysis = await modelAnalysis(body, heuristic);
  json(res, 200, analysis);
}

async function serveStatic(req, res, url) {
  const requested = url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname);
  const filePath = normalize(join(publicDir, requested));
  if (!filePath.startsWith(publicDir)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }

  try {
    const content = await readFile(filePath);
    res.writeHead(200, { "content-type": mime[extname(filePath)] || "application/octet-stream" });
    res.end(content);
  } catch {
    res.writeHead(404);
    res.end("Not found");
  }
}

createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (req.method === "GET" && url.pathname === "/api/market") {
      await handleMarket(req, res, url);
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/health") {
      handleHealth(res);
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/analyze") {
      await handleAnalyze(req, res);
      return;
    }
    if (req.method === "GET") {
      await serveStatic(req, res, url);
      return;
    }
    json(res, 405, { error: "Method not allowed" });
  } catch (error) {
    json(res, 500, { error: error.message || "Unexpected server error" });
  }
}).listen(PORT, () => {
  console.log(`L2LLM stock AI dashboard running at http://localhost:${PORT}`);
});
