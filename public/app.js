const els = {
  form: document.querySelector("#symbolForm"),
  symbol: document.querySelector("#symbolInput"),
  range: document.querySelector("#rangeSelect"),
  interval: document.querySelector("#intervalSelect"),
  providerLine: document.querySelector("#providerLine"),
  lastPrice: document.querySelector("#lastPrice"),
  changeValue: document.querySelector("#changeValue"),
  openPrice: document.querySelector("#openPrice"),
  highLow: document.querySelector("#highLow"),
  volumeValue: document.querySelector("#volumeValue"),
  portfolioUpdated: document.querySelector("#portfolioUpdated"),
  portfolioPanel: document.querySelector("#portfolioPanel"),
  portfolioToggle: document.querySelector("#portfolioToggle"),
  portfolioValue: document.querySelector("#portfolioValue"),
  portfolioPnl: document.querySelector("#portfolioPnl"),
  portfolioReturn: document.querySelector("#portfolioReturn"),
  portfolioCounts: document.querySelector("#portfolioCounts"),
  portfolioMarket: document.querySelector("#portfolioMarket"),
  marketBreakdown: document.querySelector("#marketBreakdown"),
  positionRows: document.querySelector("#positionRows"),
  tradeRows: document.querySelector("#tradeRows"),
  tradeForm: document.querySelector("#tradeForm"),
  tradeMarket: document.querySelector("#tradeMarket"),
  tradeSymbol: document.querySelector("#tradeSymbol"),
  tradeSide: document.querySelector("#tradeSide"),
  tradeQuantity: document.querySelector("#tradeQuantity"),
  tradePrice: document.querySelector("#tradePrice"),
  tradeTakeProfit: document.querySelector("#tradeTakeProfit"),
  tradeStopLoss: document.querySelector("#tradeStopLoss"),
  statusBadge: document.querySelector("#statusBadge"),
  canvas: document.querySelector("#klineCanvas"),
  secondCanvas: document.querySelector("#secondCanvas"),
  secondSource: document.querySelector("#secondSource"),
  secondPrice: document.querySelector("#secondPrice"),
  secondChange: document.querySelector("#secondChange"),
  secondCount: document.querySelector("#secondCount"),
  bids: document.querySelector("#bids"),
  asks: document.querySelector("#asks"),
  depthSource: document.querySelector("#depthSource"),
  bookNote: document.querySelector("#bookNote"),
  engineName: document.querySelector("#engineName"),
  openaiNotice: document.querySelector("#openaiNotice"),
  directionPill: document.querySelector("#directionPill"),
  summaryText: document.querySelector("#summaryText"),
  thirdPartyEngine: document.querySelector("#thirdPartyEngine"),
  thirdPartyNotice: document.querySelector("#thirdPartyNotice"),
  thirdPartyDirection: document.querySelector("#thirdPartyDirection"),
  thirdPartyConfidence: document.querySelector("#thirdPartyConfidence"),
  thirdPartySummary: document.querySelector("#thirdPartySummary"),
  thirdPartyReasons: document.querySelector("#thirdPartyReasons"),
  thirdPartyRisks: document.querySelector("#thirdPartyRisks"),
  confidence: document.querySelector("#confidence"),
  rsi: document.querySelector("#rsi"),
  volumeRatio: document.querySelector("#volumeRatio"),
  sr: document.querySelector("#sr"),
  mainAccumulation: document.querySelector("#mainAccumulation"),
  hotMoneyIgnition: document.querySelector("#hotMoneyIgnition"),
  mainChartMacd: document.querySelector("#mainChartMacd"),
  secondsMacd: document.querySelector("#secondsMacd"),
  ddeFlow: document.querySelector("#ddeFlow"),
  bullTrap: document.querySelector("#bullTrap"),
  limitUpProbability: document.querySelector("#limitUpProbability"),
  riskLevel: document.querySelector("#riskLevel"),
  klineTrend: document.querySelector("#klineTrend"),
  klinePattern: document.querySelector("#klinePattern"),
  reasonList: document.querySelector("#reasonList"),
  riskList: document.querySelector("#riskList")
};

const state = {
  timer: null,
  secondTimer: null,
  secondAbort: null,
  abort: null,
  market: null,
  realtimeSymbol: null,
  realtimeTicks: [],
  portfolioExpanded: false
};

function setPortfolioExpanded(expanded) {
  state.portfolioExpanded = expanded;
  if (!els.portfolioPanel || !els.portfolioToggle) return;
  els.portfolioPanel.classList.toggle("collapsed", !expanded);
  els.portfolioToggle.setAttribute("aria-expanded", String(expanded));
  els.portfolioToggle.textContent = expanded ? "收起" : "展开";
}

function money(value, currency = "") {
  if (!Number.isFinite(value)) return "--";
  return `${currency ? `${currency} ` : ""}${value.toLocaleString(undefined, {
    minimumFractionDigits: value > 100 ? 2 : 3,
    maximumFractionDigits: value > 100 ? 2 : 3
  })}`;
}

function compact(value) {
  if (!Number.isFinite(value)) return "--";
  return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 2 }).format(value);
}

function pct(value) {
  if (!Number.isFinite(value)) return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function formatAxisTime(value, range = els.range.value, interval = els.interval.value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";

  const minuteView = ["1m", "2m", "5m", "15m", "30m", "60m"].includes(interval);
  const longRange = ["3y", "5y", "10y", "all"].includes(range);
  const monthRange = ["1mo", "3mo", "6mo", "ytd", "1y"].includes(range);

  if (minuteView && ["1d", "5d"].includes(range)) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  if (longRange || ["1mo", "3mo", "6mo"].includes(interval)) {
    return date.toLocaleDateString([], { year: "2-digit", month: "2-digit" });
  }

  if (monthRange || ["1d", "1wk", "1w", "1week"].includes(interval)) {
    return date.toLocaleDateString([], { month: "2-digit", day: "2-digit" });
  }

  return date.toLocaleDateString([], { month: "2-digit", day: "2-digit" });
}

function signedMoney(value, currency = "") {
  if (!Number.isFinite(value)) return "--";
  return `${value >= 0 ? "+" : ""}${money(value, currency)}`;
}

function setStatus(text, tone = "") {
  els.statusBadge.textContent = text;
  els.statusBadge.className = tone;
}

function isChinaMarket(market) {
  return market?.marketType === "cn" || String(market?.marketState || "").includes("A股");
}

function marketColors(market) {
  // 统一当前产品的行情配色：上涨红色、下跌绿色。
  return isChinaMarket(market)
    ? { up: "#ef5f5f", down: "#28c47a", upClass: "cn-up", downClass: "cn-down" }
    : { up: "#ef5f5f", down: "#28c47a", upClass: "up", downClass: "down" };
}

function updateQuote(market) {
  const q = market.quote;
  const colors = marketColors(market);
  const cls = q.change >= 0 ? colors.upClass : colors.downClass;
  els.lastPrice.textContent = money(q.price, market.currency);
  els.changeValue.textContent = `${q.change >= 0 ? "+" : ""}${money(q.change)} (${pct(q.changePercent)})`;
  els.changeValue.className = cls;
  els.openPrice.textContent = money(q.open, market.currency);
  els.highLow.textContent = `${money(q.dayHigh)} / ${money(q.dayLow)}`;
  els.volumeValue.textContent = compact(q.volume);
  const displayName = market.name ? `${market.symbol} ${market.name}` : market.symbol;
  const quoteTime = market.quoteTime ? ` · 行情时间 ${market.quoteTime}` : "";
  els.providerLine.textContent = `${displayName} · ${market.exchange} · ${market.marketState} · ${market.provider}${market.delayed ? " · 可能延迟" : ""}${quoteTime} · ${new Date(market.updatedAt).toLocaleTimeString()}`;
}

function chartWindowSize(range = els.range.value, interval = els.interval.value) {
  // Long-range weekly/monthly views need more candles; a fixed 160-bar window
  // makes 10Y weekly and 10Y monthly appear to start around the same date.
  if (["5y", "10y", "all"].includes(range)) {
    if (["1wk", "1w", "1week"].includes(interval)) return 560;
    if (["1mo", "3mo", "6mo"].includes(interval)) return 240;
    return 420;
  }
  if (["1y", "3y", "ytd"].includes(range) && ["1wk", "1w", "1week"].includes(interval)) {
    return 260;
  }
  return 160;
}

function drawChart(candles, quote, market = state.market) {
  const canvas = els.canvas;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);

  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);

  if (!candles.length) {
    ctx.fillStyle = "#95a3a4";
    ctx.fillText("暂无K线数据", 24, 32);
    return;
  }

  const pad = { left: 54, right: 16, top: 18, bottom: 54 };
  const width = rect.width - pad.left - pad.right;
  const height = rect.height - pad.top - pad.bottom;
  const view = candles.slice(-chartWindowSize());
  const highs = view.map((c) => c.high);
  const lows = view.map((c) => c.low);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const span = Math.max(max - min, max * 0.002);
  const y = (price) => pad.top + (max - price) / span * height;
  const slot = width / view.length;
  const body = Math.max(2, Math.min(9, slot * 0.62));
  const colors = marketColors(market);

  ctx.strokeStyle = "#223039";
  ctx.lineWidth = 1;
  ctx.font = "12px Segoe UI, Arial";
  ctx.fillStyle = "#95a3a4";

  for (let i = 0; i <= 4; i += 1) {
    const gy = pad.top + height / 4 * i;
    const price = max - span / 4 * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, gy);
    ctx.lineTo(rect.width - pad.right, gy);
    ctx.stroke();
    ctx.fillText(price.toFixed(2), 8, gy + 4);
  }

  view.forEach((candle, index) => {
    const x = pad.left + index * slot + slot / 2;
    const up = candle.close >= candle.open;
    ctx.strokeStyle = up ? colors.up : colors.down;
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(x, y(candle.high));
    ctx.lineTo(x, y(candle.low));
    ctx.stroke();

    const top = y(Math.max(candle.open, candle.close));
    const bottom = y(Math.min(candle.open, candle.close));
    ctx.fillRect(x - body / 2, top, body, Math.max(1.5, bottom - top));
  });

  const closes = view.map((c) => c.close);
  drawAverage(ctx, view, closes, 20, pad, width, y, "#e3b341");
  drawAverage(ctx, view, closes, 60, pad, width, y, "#54b6d6");

  const volumes = view.map((c) => c.volume || 0);
  const maxVolume = Math.max(...volumes, 1);
  const volumeTop = rect.height - 42;
  view.forEach((candle, index) => {
    const x = pad.left + index * slot + slot / 2;
    const barHeight = (candle.volume || 0) / maxVolume * 34;
    ctx.fillStyle = candle.close >= candle.open
      ? `${colors.up}45`
      : `${colors.down}45`;
    ctx.fillRect(x - body / 2, volumeTop - barHeight, body, barHeight);
  });

  if (Number.isFinite(quote.price)) {
    const py = y(quote.price);
    ctx.strokeStyle = "#eef4f2";
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(rect.width - pad.right, py);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  const axisY = pad.top + height + 18;
  const tickCount = Math.min(7, Math.max(3, Math.floor(width / 120)));
  const tickIndexes = Array.from({ length: tickCount }, (_, i) => {
    if (tickCount === 1) return 0;
    return Math.round(i * (view.length - 1) / (tickCount - 1));
  });
  ctx.strokeStyle = "#2e3f48";
  ctx.fillStyle = "#b4c0c0";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top + height);
  ctx.lineTo(rect.width - pad.right, pad.top + height);
  ctx.stroke();
  [...new Set(tickIndexes)].forEach((index) => {
    const candle = view[index];
    const label = formatAxisTime(candle.time);
    if (!label) return;
    const x = pad.left + index * slot + slot / 2;
    ctx.beginPath();
    ctx.moveTo(x, pad.top + height);
    ctx.lineTo(x, pad.top + height + 5);
    ctx.stroke();
    ctx.fillText(label, x, axisY);
  });
  ctx.textAlign = "start";
  ctx.textBaseline = "alphabetic";
}

function drawAverage(ctx, candles, closes, size, pad, width, y, color) {
  if (candles.length < size) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let started = false;
  candles.forEach((_, index) => {
    if (index + 1 < size) return;
    const avg = closes.slice(index + 1 - size, index + 1).reduce((a, b) => a + b, 0) / size;
    const x = pad.left + index * (width / candles.length) + (width / candles.length) / 2;
    const yy = y(avg);
    if (!started) {
      ctx.moveTo(x, yy);
      started = true;
    } else {
      ctx.lineTo(x, yy);
    }
  });
  ctx.stroke();
}

function todayKey(timestamp) {
  return new Date(timestamp).toLocaleDateString();
}

function resetRealtime(symbol) {
  state.realtimeSymbol = symbol;
  state.realtimeTicks = [];
  drawSecondChart();
}

function drawSecondChart() {
  const canvas = els.secondCanvas;
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const pad = { top: 16, right: 56, bottom: 24, left: 52 };
  const width = rect.width - pad.left - pad.right;
  const height = rect.height - pad.top - pad.bottom;
  ctx.strokeStyle = "#223039";
  ctx.lineWidth = 1;
  ctx.strokeRect(pad.left, pad.top, width, height);

  const ticks = state.realtimeTicks.filter((tick) => Number.isFinite(tick.price));
  if (ticks.length < 2) {
    ctx.fillStyle = "#95a3a4";
    ctx.font = "12px Segoe UI";
    ctx.fillText("等待秒级行情...", pad.left + 12, pad.top + 24);
    return;
  }

  const prices = ticks.map((tick) => tick.price);
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const spread = Math.max(max - min, Math.abs(max) * 0.0005, 0.01);
  const y = (price) => pad.top + (max + spread * 0.15 - price) / (spread * 1.3) * height;
  const x = (index) => pad.left + (index / Math.max(ticks.length - 1, 1)) * width;
  const colors = marketColors(state.market || {});

  ctx.strokeStyle = colors.up;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ticks.forEach((tick, index) => {
    const px = x(index);
    const py = y(tick.price);
    if (index === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });
  ctx.stroke();

  const last = ticks[ticks.length - 1];
  ctx.fillStyle = colors.up;
  ctx.beginPath();
  ctx.arc(x(ticks.length - 1), y(last.price), 3, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#95a3a4";
  ctx.font = "11px Segoe UI";
  ctx.fillText(max.toFixed(2), pad.left + width + 8, pad.top + 4);
  ctx.fillText(min.toFixed(2), pad.left + width + 8, pad.top + height);
  ctx.fillText(new Date(ticks[0].time).toLocaleTimeString([], { hour12: false }), pad.left, pad.top + height + 18);
  ctx.fillText(new Date(last.time).toLocaleTimeString([], { hour12: false }), pad.left + width - 56, pad.top + height + 18);
}

async function fetchRealtimeTick() {
  const symbol = els.symbol.value.trim().toUpperCase() || "AAPL";
  if (state.realtimeSymbol !== symbol) resetRealtime(symbol);
  state.secondAbort?.abort();
  state.secondAbort = new AbortController();
  try {
    const response = await fetch(`/api/realtime?symbol=${encodeURIComponent(symbol)}`, { signal: state.secondAbort.signal });
    const tick = await response.json();
    if (!response.ok) throw new Error(tick.detail || tick.error || "秒级行情失败");
    const parsedTime = tick.quoteTime ? Date.parse(tick.quoteTime) : Date.parse(tick.updatedAt);
    const timeValue = Number.isFinite(parsedTime) ? parsedTime : Date.now();
    const currentDay = todayKey(Date.now());
    const sameDayTicks = state.realtimeTicks.filter((item) => todayKey(item.time) === currentDay);
    const nextTick = { ...tick, time: timeValue, price: Number(tick.price) };
    const previous = sameDayTicks[sameDayTicks.length - 1];
    state.realtimeTicks = [...sameDayTicks, nextTick].slice(-7200);
    if (previous && previous.time === nextTick.time && previous.price === nextTick.price) {
      state.realtimeTicks[state.realtimeTicks.length - 1].time = Date.now();
    }
    els.secondSource.textContent = tick.provider || "--";
    els.secondPrice.textContent = money(nextTick.price, tick.currency || state.market?.currency || "");
    els.secondChange.textContent = `${Number(tick.change) >= 0 ? "+" : ""}${money(Number(tick.change))} (${pct(Number(tick.changePercent))})`;
    els.secondChange.className = Number(tick.change) >= 0 ? marketColors(state.market || tick).upClass : marketColors(state.market || tick).downClass;
    els.secondCount.textContent = `${state.realtimeTicks.length} ticks`;
    drawSecondChart();
  } catch (error) {
    if (error.name === "AbortError") return;
    if (els.secondSource) els.secondSource.textContent = error.message;
  }
}

function renderBook(orderBook, currency) {
  const maxSize = Math.max(
    ...orderBook.bids.map((row) => row.size),
    ...orderBook.asks.map((row) => row.size),
    1
  );
  const row = (item, side) => `
    <div class="book-row ${side}" style="--depth:${Math.max(6, item.size / maxSize * 100).toFixed(1)}%">
      <span>${money(item.price, currency)}</span>
      <span>${compact(item.size)}</span>
    </div>
  `;
  els.bids.innerHTML = orderBook.bids.map((item) => row(item, "bid")).join("");
  els.asks.innerHTML = orderBook.asks.map((item) => row(item, "ask")).join("");
  els.depthSource.textContent = orderBook.provider || "unknown";
  els.bookNote.textContent = orderBook.note || "";
}

function renderList(target, items) {
  target.innerHTML = "";
  for (const item of items || []) {
    const li = document.createElement("li");
    li.textContent = item;
    target.appendChild(li);
  }
}

function toneClass(value, market = state.market) {
  const colors = marketColors(market);
  return value >= 0 ? colors.upClass : colors.downClass;
}

function renderPortfolio(portfolio) {
  const summary = portfolio.summary || {};
  const currency = portfolio.positions?.[0]?.currency || state.market?.currency || "";
  els.portfolioUpdated.textContent = portfolio.updatedAt ? new Date(portfolio.updatedAt).toLocaleTimeString() : "--";
  els.portfolioValue.textContent = money(summary.marketValue, currency);
  els.portfolioPnl.textContent = signedMoney(summary.unrealizedPnl, currency);
  els.portfolioPnl.className = toneClass(summary.unrealizedPnl || 0);
  els.portfolioReturn.textContent = pct(summary.totalReturn);
  els.portfolioReturn.className = toneClass(summary.totalReturn || 0);
  els.portfolioCounts.textContent = `${summary.positionCount || 0} / ${summary.tradeCount || 0}`;
  els.marketBreakdown.innerHTML = "";
  for (const key of ["cn", "us", "hk"]) {
    const item = portfolio.markets?.[key];
    if (!item) continue;
    const div = document.createElement("div");
    div.innerHTML = `
      <span>${item.label}</span>
      <strong>${money(item.marketValue, item.currency)}</strong>
      <em class="${toneClass(item.totalReturn || 0)}">${pct(item.totalReturn)}</em>
    `;
    els.marketBreakdown.appendChild(div);
  }

  els.positionRows.innerHTML = "";
  for (const row of portfolio.positions || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.marketLabel || row.market}</td>
      <td>${row.symbol}<small>${row.name || ""}</small></td>
      <td>${compact(row.quantity)}</td>
      <td>${money(row.avgCost, row.currency)}</td>
      <td>${money(row.currentPrice, row.currency)}</td>
      <td class="${toneClass(row.unrealizedReturn || 0)}">${pct(row.unrealizedReturn)}</td>
      <td>${Number.isFinite(row.weight) ? row.weight.toFixed(1) : "--"}%</td>
    `;
    els.positionRows.appendChild(tr);
  }
  if (!portfolio.positions?.length) {
    els.positionRows.innerHTML = `<tr><td colspan="7">暂无持仓</td></tr>`;
  }

  els.tradeRows.innerHTML = "";
  for (const row of portfolio.trades || []) {
    const takeProfitText = row.takeProfit ? `${money(row.takeProfit)} (${pct(row.distanceToTakeProfit)})` : "--";
    const stopLossText = row.stopLoss ? `${money(row.stopLoss)} (${pct(row.distanceToStopLoss)})` : "--";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.marketLabel || row.market}</td>
      <td>${row.symbol}</td>
      <td>${row.side === "buy" ? "买入" : "卖出"}</td>
      <td>${money(row.price)}</td>
      <td>${money(row.currentPrice)}</td>
      <td>${takeProfitText}</td>
      <td>${stopLossText}</td>
    `;
    els.tradeRows.appendChild(tr);
  }
  if (!portfolio.trades?.length) {
    els.tradeRows.innerHTML = `<tr><td colspan="7">暂无交易记录</td></tr>`;
  }
}

async function fetchPortfolio(signal) {
  const market = els.portfolioMarket?.value || "all";
  const response = await fetch(`/api/portfolio?market=${encodeURIComponent(market)}`, { signal });
  if (!response.ok) throw new Error("仓位加载失败");
  return response.json();
}

async function addTrade(event) {
  event.preventDefault();
  const payload = {
    market: els.tradeMarket.value,
    symbol: els.tradeSymbol.value.trim() || els.symbol.value.trim(),
    side: els.tradeSide.value,
    quantity: Number(els.tradeQuantity.value),
    price: Number(els.tradePrice.value),
    takeProfit: els.tradeTakeProfit.value ? Number(els.tradeTakeProfit.value) : null,
    stopLoss: els.tradeStopLoss.value ? Number(els.tradeStopLoss.value) : null
  };
  const response = await fetch("/api/portfolio/trades", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload)
  });
  const portfolio = await response.json();
  if (!response.ok) throw new Error(portfolio.detail || portfolio.error || "交易保存失败");
  renderPortfolio(portfolio);
  if (els.portfolioMarket.value !== payload.market) {
    els.portfolioMarket.value = payload.market;
  }
  els.tradeForm.reset();
  els.tradeMarket.value = payload.market;
}

function signalText(signal, suffix = "") {
  if (!signal) return "--";
  const score = Number.isFinite(signal.score) ? `${signal.score}${suffix}` : "--";
  return `${signal.label || "--"} · ${score}`;
}

function decisionTone(text = "") {
  return text.includes("多") || text.includes("å¤š")
    ? "bullish"
    : text.includes("空") || text.includes("ç©º")
      ? "bearish"
      : "neutral";
}

function renderAnalysis(analysis) {
  const local = analysis.local || analysis;
  const thirdParty = analysis.thirdParty || analysis.external || null;
  const text = local.direction || "--";
  els.directionPill.textContent = text;
  els.directionPill.className = `direction ${decisionTone(text)}`;
  els.engineName.textContent = "本地AI";

  const aiStatus = thirdParty?.aiStatus || analysis.aiStatus || analysis.openaiStatus;
  const aiReason = thirdParty?.aiReason || analysis.aiReason || analysis.openaiReason;
  if (aiStatus && aiStatus !== "ok") {
    els.openaiNotice.hidden = false;
    els.openaiNotice.textContent = `第三方API未生效：${aiReason || aiStatus}`;
  } else {
    els.openaiNotice.hidden = true;
    els.openaiNotice.textContent = "";
  }

  els.summaryText.textContent = local.summary || "--";
  els.confidence.textContent = local.confidence ? `${local.confidence}%` : "--";
  els.rsi.textContent = Number.isFinite(local.metrics?.rsi14) ? local.metrics.rsi14.toFixed(1) : "--";
  els.volumeRatio.textContent = Number.isFinite(local.metrics?.volumeRatio) ? `${local.metrics.volumeRatio.toFixed(2)}x` : "--";
  els.sr.textContent = `${money(local.metrics?.support)} / ${money(local.metrics?.resistance)}`;
  els.mainAccumulation.textContent = signalText(local.signals?.mainAccumulation, "%");
  els.hotMoneyIgnition.textContent = signalText(local.signals?.hotMoneyIgnition, "%");
  els.mainChartMacd.textContent = signalText(local.signals?.mainChartMacd, "");
  els.secondsMacd.textContent = signalText(local.signals?.secondsMacd, "");
  els.ddeFlow.textContent = signalText(local.signals?.ddeFlow, "%");
  els.bullTrap.textContent = signalText(local.signals?.bullTrap, "%");
  els.limitUpProbability.textContent = signalText(local.signals?.limitUpProbability, "%");
  els.riskLevel.textContent = signalText(local.signals?.riskLevel, "");
  els.klineTrend.textContent = signalText(local.kline?.trend, "%");
  const topPattern = local.kline?.patterns?.[0];
  els.klinePattern.textContent = topPattern ? `${topPattern.name} Â· ${topPattern.direction}` : "--";
  renderList(els.reasonList, local.reasons);
  renderList(els.riskList, local.risks);

  if (els.thirdPartyEngine) {
    const provider = thirdParty?.aiProvider || thirdParty?.engine;
    els.thirdPartyEngine.textContent = provider === "gemini" ? "Gemini" : provider === "openai" ? "OpenAI / ChatGPT" : "第三方API";
    const externalDirection = thirdParty?.direction || "--";
    els.thirdPartyDirection.textContent = externalDirection;
    els.thirdPartyDirection.className = `direction ${decisionTone(externalDirection)}`;
    els.thirdPartyConfidence.textContent = thirdParty?.confidence ? `${thirdParty.confidence}%` : "--";
    els.thirdPartySummary.textContent = thirdParty?.summary || "第三方API未返回独立判断。";
    if (thirdParty?.aiStatus && thirdParty.aiStatus !== "ok") {
      els.thirdPartyNotice.hidden = false;
      els.thirdPartyNotice.textContent = thirdParty.aiReason || thirdParty.aiStatus;
    } else {
      els.thirdPartyNotice.hidden = true;
      els.thirdPartyNotice.textContent = "";
    }
    renderList(els.thirdPartyReasons, thirdParty?.reasons);
    renderList(els.thirdPartyRisks, thirdParty?.risks);
  }
}

async function fetchAnalysis(market, signal) {
  const response = await fetch("/api/analyze", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      symbol: market.symbol,
      marketType: market.marketType,
      range: els.range.value,
      interval: els.interval.value,
      quote: market.quote,
      candles: market.candles,
      orderBook: market.orderBook,
      fundFlow: market.fundFlow,
      realtimeTicks: state.realtimeTicks
    }),
    signal
  });
  if (!response.ok) throw new Error("分析失败");
  return response.json();
}

async function refresh() {
  state.abort?.abort();
  state.abort = new AbortController();
  const symbol = els.symbol.value.trim().toUpperCase() || "AAPL";
  els.symbol.value = symbol;
  setStatus("刷新中");

  try {
    const url = `/api/market?symbol=${encodeURIComponent(symbol)}&range=${els.range.value}&interval=${els.interval.value}`;
    const response = await fetch(url, { signal: state.abort.signal });
    const market = await response.json();
    if (!response.ok) throw new Error(market.detail || market.error || "行情加载失败");
    state.market = market;

    updateQuote(market);
    drawChart(market.candles, market.quote, market);
    renderBook(market.orderBook, market.currency);
    setStatus("实时刷新", "up");

    const analysis = await fetchAnalysis(market, state.abort.signal);
    renderAnalysis(analysis);
    const portfolio = await fetchPortfolio(state.abort.signal);
    renderPortfolio(portfolio);
  } catch (error) {
    if (error.name === "AbortError") return;
    setStatus("连接异常", "down");
    els.providerLine.textContent = error.message;
  }
}

function schedule() {
  clearInterval(state.timer);
  state.timer = setInterval(refresh, 15000);
  clearInterval(state.secondTimer);
  fetchRealtimeTick();
  state.secondTimer = setInterval(fetchRealtimeTick, 1000);
}

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  refresh();
  schedule();
});

els.tradeForm.addEventListener("submit", (event) => {
  addTrade(event).catch((error) => {
    setStatus("交易保存失败", "down");
    els.providerLine.textContent = error.message;
  });
});

els.portfolioMarket.addEventListener("change", () => {
  fetchPortfolio().then(renderPortfolio).catch((error) => {
    setStatus("仓位加载失败", "down");
    els.providerLine.textContent = error.message;
  });
});

els.portfolioToggle?.addEventListener("click", () => {
  setPortfolioExpanded(!state.portfolioExpanded);
});

els.range.addEventListener("change", refresh);
els.interval.addEventListener("change", refresh);
window.addEventListener("resize", () => {
  if (state.market) drawChart(state.market.candles, state.market.quote, state.market);
  drawSecondChart();
});

setPortfolioExpanded(false);
refresh();
schedule();
