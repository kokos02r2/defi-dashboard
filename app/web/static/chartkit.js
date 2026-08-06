/* Общее для всех графиков: цвета из CSS-переменных, форматирование и выбор периода.
   Вынесено из шаблонов, чтобы график позиции и график портфеля вели себя одинаково. */

window.ChartKit = (function () {

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function usd(v, digits = 2) {
    if (v === null || v === undefined || !Number.isFinite(v)) return "н/д";
    const sign = v < 0 ? "-" : "";
    return sign + "$" + Math.abs(v).toLocaleString("ru-RU",
      {minimumFractionDigits: digits, maximumFractionDigits: digits});
  }

  /* Сумма в произвольной валюте — для личных финансов, где база не доллар.
     Знак ставится так же, как в серверном фильтре money: у доллара и рубля впереди,
     у остальных после числа. Иначе подпись на графике и цифра в таблице выглядели бы
     как разные величины. */
  const SYMBOLS = {EUR: "€", USD: "$", RUB: "₽", GBP: "£", CHF: "Fr",
                   TRY: "₺", KZT: "₸", GEL: "₾", RSD: "din", AED: "dh"};

  function money(v, code, digits = 2) {
    if (v === null || v === undefined || !Number.isFinite(v)) return "н/д";
    const sym = SYMBOLS[code] || code || "";
    const sign = v < 0 ? "-" : "";
    const num = Math.abs(v).toLocaleString("ru-RU",
      {minimumFractionDigits: digits, maximumFractionDigits: digits});
    return (code === "USD" || code === "RUB") ? sign + sym + num : sign + num + " " + sym;
  }

  /**
   * Формат подписей оси Y подбирается под РАЗБРОС значений, а не под их величину.
   * Портфель, который весь день стоит около 12 000, при округлении до тысяч даёт
   * восемь одинаковых «$12k» — ось перестаёт что-либо значить.
   */
  function tickFormatter(values, code) {
    // Валюта параметром, а не жёстко доллар: те же оси рисуют личные расходы в евро,
    // и подпись «$1 200» там была бы просто неверной.
    const cur = code || "USD";
    const sym = SYMBOLS[cur] || cur;
    const before = cur === "USD" || cur === "RUB";
    const put = (v, body) => {
      const s = v < 0 ? "-" : "";
      const b = body(Math.abs(v));
      return before ? s + sym + b : s + b + " " + sym;
    };

    const nums = values.filter(v => v !== null && v !== undefined && Number.isFinite(v));
    if (!nums.length) return v => put(v, a => String(Math.round(a)));
    const spread = Math.max(...nums) - Math.min(...nums);
    const scale = Math.max(...nums.map(Math.abs));

    if (spread >= 20000 || (spread === 0 && scale >= 20000)) {
      return v => put(v, a => a >= 1e6 ? (a / 1e6).toFixed(1) + "M"
                                       : Math.round(a / 1000) + "k");
    }
    if (spread >= 2000) {
      return v => put(v, a => Math.round(a).toLocaleString("ru-RU"));
    }
    if (spread >= 20) {
      return v => put(v, a => a.toLocaleString("ru-RU",
        {minimumFractionDigits: 0, maximumFractionDigits: 0}));
    }
    // совсем узкий коридор — иначе все подписи схлопнутся в одно число
    return v => put(v, a => a.toLocaleString("ru-RU",
      {minimumFractionDigits: 2, maximumFractionDigits: 2}));
  }

  /** Подписи по оси X: чем шире период, тем крупнее шаг. */
  function labelFormatter(days) {
    if (days <= 2) {
      return d => d.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
    }
    if (days <= 7) {
      return d => d.toLocaleString("ru-RU",
        {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"});
    }
    return d => d.toLocaleDateString("ru-RU", {day: "2-digit", month: "short"});
  }

  /** Стиль линии. При одной-двух точках линию провести не из чего — показываем точки. */
  function mark(color, pointCount) {
    const surface = css("--surface");
    return {
      borderColor: color, borderWidth: 2, tension: .25,
      pointRadius: pointCount <= 3 ? 4 : 0, pointHoverRadius: 5,
      pointBackgroundColor: color,
      pointBorderColor: surface, pointBorderWidth: 2,
      pointHoverBorderColor: surface, pointHoverBorderWidth: 2,
    };
  }

  /** Общие настройки осей и подсказки. */
  function options(allValues, labelFn) {
    const surface = css("--surface"), muted = css("--ink-muted");
    return {
      responsive: true, maintainAspectRatio: false,
      // курсор ведёт по всей вертикали — значение читается в любой точке графика
      interaction: {mode: "index", intersect: false},
      plugins: {
        legend: {display: false},   // легенда своя, в HTML — чтобы совпадала со стилем
        tooltip: {
          backgroundColor: surface, titleColor: css("--ink"), bodyColor: css("--ink-2"),
          borderColor: css("--hairline"), borderWidth: 1, padding: 10,
          displayColors: true, boxWidth: 8, boxHeight: 8, usePointStyle: true,
          callbacks: {label: labelFn},
        },
      },
      scales: {
        x: {
          grid: {display: false},
          border: {color: css("--axis")},
          ticks: {color: muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 8,
                  font: {size: 11}},
        },
        y: {
          // сплошные волосяные линии, без пунктира
          grid: {color: css("--grid"), drawTicks: false},
          border: {display: false},
          ticks: {color: muted, font: {size: 11}, padding: 8,
                  callback: tickFormatter(allValues)},
        },
      },
    };
  }

  /** Подпись под заголовком: сколько точек набралось. */
  function pointsNote(n, isOpen) {
    if (!n) {
      return isOpen === false ? "истории нет — позиция закрыта"
                              : "истории пока нет, первая точка появится после обновления";
    }
    if (n === 1) return "пока одна точка, график наберётся за несколько часов";
    if (n === 2) return "точек ещё мало";
    return n + " точек";
  }

  /** Кнопки выбора периода. onChange получает число дней. */
  function rangeButtons(root, onChange) {
    root.querySelectorAll("button[data-days]").forEach(b => {
      b.addEventListener("click", () => {
        root.querySelectorAll("button[data-days]")
            .forEach(x => x.setAttribute("aria-pressed", "false"));
        b.setAttribute("aria-pressed", "true");
        onChange(parseInt(b.dataset.days, 10));
      });
    });
  }

  return {css, usd, money, tickFormatter, labelFormatter, mark, options, pointsNote,
          rangeButtons};
})();
