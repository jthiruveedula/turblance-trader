// Hero tape: scrolling candlesticks with dealer-exposure (GEX) bars.
(function () {
  const canvas = document.getElementById('tape');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W, H, candles = [], gexLevels = [];
  const N = 60;

  function resize() {
    W = canvas.width = canvas.offsetWidth * devicePixelRatio;
    H = canvas.height = canvas.offsetHeight * devicePixelRatio;
  }
  window.addEventListener('resize', resize);
  resize();

  function seed() {
    let price = 0.5;
    candles = [];
    for (let i = 0; i < N; i++) {
      const drift = (Math.random() - 0.48) * 0.03;
      const open = price;
      const close = open + drift;
      const high = Math.max(open, close) + Math.random() * 0.02;
      const low = Math.min(open, close) - Math.random() * 0.02;
      candles.push({ open, close, high, low });
      price = close;
    }
    // GEX walls: a few strong levels, mostly positive (green) below, negative (red) above
    gexLevels = [];
    const walls = 4 + Math.floor(Math.random() * 3);
    for (let i = 0; i < walls; i++) {
      gexLevels.push({
        level: 0.15 + Math.random() * 0.7,
        mag: 0.3 + Math.random() * 0.7,
        pos: Math.random() > 0.35
      });
    }
  }
  seed();

  function tick() {
    // advance one candle
    const last = candles[candles.length - 1];
    const drift = (Math.random() - 0.48) * 0.03;
    const open = last.close;
    const close = open + drift;
    candles.push({
      open, close,
      high: Math.max(open, close) + Math.random() * 0.02,
      low: Math.min(open, close) - Math.random() * 0.02
    });
    if (candles.length > N) candles.shift();
    if (Math.random() < 0.06) seedGex();
  }

  function seedGex() {
    gexLevels.push({
      level: 0.15 + Math.random() * 0.7,
      mag: 0.3 + Math.random() * 0.7,
      pos: Math.random() > 0.35
    });
    if (gexLevels.length > 7) gexLevels.shift();
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    const dpr = devicePixelRatio;
    const cw = W / N;
    const yOf = v => H - v * H * 0.9 - H * 0.05;

    // GEX bars behind candles
    gexLevels.forEach(g => {
      const y = yOf(g.level);
      const w = g.mag * W * 0.55;
      const grad = ctx.createLinearGradient(W - w, 0, W, 0);
      const col = g.pos ? '0,229,160' : '255,93,108';
      grad.addColorStop(0, `rgba(${col},0)`);
      grad.addColorStop(1, `rgba(${col},0.16)`);
      ctx.fillStyle = grad;
      ctx.fillRect(W - w, y - 3 * dpr, w, 6 * dpr);
      ctx.fillStyle = `rgba(${col},0.5)`;
      ctx.fillRect(W - 2 * dpr, y - 3 * dpr, 2 * dpr, 6 * dpr);
    });

    // gridlines
    ctx.strokeStyle = 'rgba(27,39,64,0.6)';
    ctx.lineWidth = 1;
    for (let i = 1; i < 5; i++) {
      ctx.beginPath();
      ctx.moveTo(0, (H / 5) * i);
      ctx.lineTo(W, (H / 5) * i);
      ctx.stroke();
    }

    // candles
    const bw = Math.max(2, cw * 0.55);
    candles.forEach((c, i) => {
      const x = i * cw + cw / 2;
      const up = c.close >= c.open;
      ctx.strokeStyle = up ? 'rgba(0,229,160,0.75)' : 'rgba(255,93,108,0.75)';
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = Math.max(1, dpr);
      ctx.beginPath();
      ctx.moveTo(x, yOf(c.high));
      ctx.lineTo(x, yOf(c.low));
      ctx.stroke();
      const yO = yOf(c.open), yC = yOf(c.close);
      ctx.fillRect(x - bw / 2, Math.min(yO, yC), bw, Math.max(2, Math.abs(yC - yO)));
    });

    // fade edges
    const fade = ctx.createLinearGradient(0, 0, W, 0);
    fade.addColorStop(0, 'rgba(7,11,18,1)');
    fade.addColorStop(0.15, 'rgba(7,11,18,0)');
    fade.addColorStop(0.85, 'rgba(7,11,18,0)');
    fade.addColorStop(1, 'rgba(7,11,18,1)');
    ctx.fillStyle = fade;
    ctx.fillRect(0, 0, W, H);
  }

  setInterval(() => { tick(); draw(); }, 900);
  draw();
})();
