// Content script: injected automatically (see manifest.json content_scripts)
// on every Steam Community Market listing page, docks a collapsible sidebar
// to the right edge, and keeps it stocked with the mono-trade price table
// for whichever skin that page is showing -- no popup click needed.
//
// Runs in an isolated world (Firefox content-script convention), so nothing
// here can see or be seen by the page's own JS -- but browser.runtime
// APIs are still limited compared to a background/extension page, notably
// no sendNativeMessage, so every fetch is relayed through background.js.
(function () {
  if (window.__bdSidebarInjected) return;
  window.__bdSidebarInjected = true;

  const CS2_LISTING_PATTERN = /^https:\/\/steamcommunity\.com\/market\/listings\/730\//;
  if (!CS2_LISTING_PATTERN.test(window.location.href)) return;

  // --- Page scraping (see webext/popup.js's original scrapeListingPage for
  // the full rationale behind text/DOM-structure matching instead of class
  // names -- unchanged here, just no longer wrapped for executeScript
  // injection since this file already runs in the page itself). ---------

  const WEAR_NAMES = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"];
  const WEAR_SUFFIX_RE = new RegExp("\\((" + WEAR_NAMES.join("|") + ")\\)$");
  const SYMBOL_TO_CURRENCY = { "€": "EUR", "$": "USD", "£": "GBP" };

  function textOf(el) {
    return (el.textContent || "").trim();
  }

  function findNameSpan(root) {
    const spans = root.querySelectorAll("span");
    for (const span of spans) {
      if (span.children.length === 0 && WEAR_SUFFIX_RE.test(textOf(span))) {
        return span;
      }
    }
    return null;
  }

  function findCard(labelEl) {
    let node = labelEl.parentElement;
    while (node) {
      const hasBuyButton = Array.from(node.querySelectorAll("button")).some((b) => textOf(b) === "Buy");
      const nameSpan = hasBuyButton ? findNameSpan(node) : null;
      if (hasBuyButton && nameSpan) {
        return { root: node, fullName: textOf(nameSpan) };
      }
      node = node.parentElement;
    }
    return null;
  }

  function extractLabelValue(cardRoot, label) {
    const divs = cardRoot.querySelectorAll("div");
    for (const div of divs) {
      if (textOf(div).startsWith(label + ":")) {
        const span = div.querySelector("span");
        if (span) return textOf(span);
      }
    }
    return null;
  }

  function extractPriceText(cardRoot) {
    const buyButton = Array.from(cardRoot.querySelectorAll("button")).find((b) => textOf(b) === "Buy");
    if (!buyButton) return null;
    let sibling = buyButton.previousElementSibling;
    while (sibling && !/[0-9]/.test(textOf(sibling))) {
      sibling = sibling.previousElementSibling;
    }
    return sibling ? textOf(sibling) : null;
  }

  // Buy-order-book summary ("2.302 requests to buy at €143,65 or lower") --
  // Steam only renders this line once a wear filter is active on the page.
  // Number formatting follows the page's locale in OPPOSITE roles from the
  // price parsing below: a EUR-locale page uses '.' as the *count*'s
  // thousands separator and ',' as the *price*'s decimal separator, while a
  // USD-locale page uses ',' for the count's thousands and '.' for the
  // price's decimal.
  function findBuyOrderSummary(currency) {
    if (currency !== "USD" && currency !== "EUR") return null;
    const thousandsSep = currency === "EUR" ? "." : ",";
    const decimalSep = currency === "EUR" ? "," : ".";
    const re = /([\d.,]+)\s+requests?\s+to\s+buy\s+at\s+[^\d\s]*\s*([\d.,]+)\s+or\s+lower/i;
    const spans = document.querySelectorAll("span");
    for (const span of spans) {
      if (span.children.length > 0) continue;
      const text = textOf(span).replace(/\u00a0/g, " ");
      const m = text.match(re);
      if (!m) continue;
      const countStr = m[1].split(thousandsSep).join("");
      const priceStr = m[2].split(thousandsSep).join("").replace(decimalSep, ".");
      const numOrders = parseInt(countStr, 10);
      const price = parseFloat(priceStr);
      if (Number.isNaN(numOrders) || Number.isNaN(price)) continue;
      return { num_orders: numOrders, price };
    }
    return null;
  }

  // Which wear the page is currently filtered to, if any -- read from the
  // "Exterior: <wear>" active-filter chip Steam's Filters UI renders.
  function findActiveWearFilter() {
    const re = /^Exterior:\s*(Factory New|Minimal Wear|Field-Tested|Well-Worn|Battle-Scarred)$/;
    const candidates = document.querySelectorAll("a, div, span");
    for (const el of candidates) {
      if (el.children.length > 2) continue;
      const text = textOf(el).replace(/\s+/g, " ").trim();
      const m = text.match(re);
      if (m) return m[1];
    }
    return null;
  }

  function findWalletCurrency() {
    try {
      if (window.g_rgWalletInfo && window.g_rgWalletInfo.wallet_currency_display) {
        return { currency: window.g_rgWalletInfo.wallet_currency_display, source: "wallet_info" };
      }
    } catch (e) {
      // ignore -- fall through to symbol guessing below
    }
    return null;
  }

  function scrapePage() {
    const wearLabels = Array.from(document.querySelectorAll("div")).filter((div) =>
      textOf(div).startsWith("Wear Rating:")
    );

    const seenRoots = new Set();
    const offers = [];
    let symbolGuess = null;
    let representativeName = null;

    for (const label of wearLabels) {
      const found = findCard(label);
      if (!found || seenRoots.has(found.root)) continue;
      seenRoots.add(found.root);
      const cardRoot = found.root;

      const wearText = extractLabelValue(cardRoot, "Wear Rating");
      const patternText = extractLabelValue(cardRoot, "Pattern Template");
      const priceText = extractPriceText(cardRoot);
      if (!wearText || !priceText) continue;

      const floatValue = parseFloat(wearText.replace(",", "."));
      if (Number.isNaN(floatValue)) continue;

      const patternSeed = patternText ? parseInt(patternText, 10) : null;

      const priceMatch = priceText.match(/([0-9]+(?:[.,][0-9]+)?)/);
      if (!priceMatch) continue;
      const price = parseFloat(priceMatch[1].replace(",", "."));

      if (!symbolGuess) {
        const symbolChar = Object.keys(SYMBOL_TO_CURRENCY).find((s) => priceText.includes(s));
        if (symbolChar) symbolGuess = SYMBOL_TO_CURRENCY[symbolChar];
      }

      const wearMatch = found.fullName.match(WEAR_SUFFIX_RE);
      if (!representativeName) representativeName = found.fullName;

      offers.push({
        wear_name: wearMatch ? wearMatch[1] : null,
        float_value: floatValue,
        pattern_seed: Number.isNaN(patternSeed) ? null : patternSeed,
        price: price,
      });
    }

    const walletCurrency = findWalletCurrency();
    const currency = walletCurrency ? walletCurrency.currency : symbolGuess;
    const currencySource = walletCurrency ? walletCurrency.source : "symbol_guess";

    const activeWear = findActiveWearFilter();
    const buyOrder = findBuyOrderSummary(currency);
    const buyOrderSummary =
      activeWear && buyOrder ? { wear_name: activeWear, price: buyOrder.price, num_orders: buyOrder.num_orders } : null;

    return {
      market_hash_name: representativeName,
      currency,
      currency_source: currencySource,
      offers,
      buy_order_summary: buyOrderSummary,
    };
  }

  // --- Native host relay (via background.js -- see its own comment) ------

  async function sendScrapeToHost(payload) {
    const response = await browser.runtime.sendMessage({ type: "fetchOffers", payload });
    if (!response.ok) throw new Error(response.error);
    return response.reply;
  }

  // --- Sidebar shell + rendering -------------------------------------------

  const els = {};

  function fmtMoney(value) {
    if (value === null || value === undefined) return "—"; // em dash
    const sign = value < 0 ? "-" : "";
    return `${sign}$${Math.abs(value).toFixed(2)}`;
  }

  function setStatus(text, cls) {
    els.status.textContent = text;
    els.status.className = cls || "";
  }

  function makeHeaderCell(text) {
    const th = document.createElement("th");
    th.textContent = text;
    return th;
  }

  function makeSkinHeaderCell(header) {
    const th = document.createElement("th");
    const a = document.createElement("a");
    a.href = header.steam_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = header.skin_name;
    th.appendChild(a);
    return th;
  }

  function makePriceCell(cell) {
    const td = document.createElement("td");
    td.textContent = fmtMoney(cell.value);
    if (cell.color) td.classList.add("bd-c-" + cell.color);
    return td;
  }

  function renderTable(table) {
    els.tableWrap.textContent = "";
    if (!table) return;

    const el = document.createElement("table");

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    headRow.appendChild(makeHeaderCell("Wear"));
    headRow.appendChild(makeSkinHeaderCell(table.input_header));
    for (const header of table.outcome_headers) headRow.appendChild(makeSkinHeaderCell(header));
    headRow.appendChild(makeHeaderCell("EV"));
    thead.appendChild(headRow);
    el.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const row of table.rows) {
      const tr = document.createElement("tr");
      const wearTd = document.createElement("td");
      wearTd.textContent = row.wear_name;
      tr.appendChild(wearTd);
      tr.appendChild(makePriceCell(row.input_cell));
      for (const cell of row.outcome_cells) tr.appendChild(makePriceCell(cell));
      const evTd = makePriceCell(row.ev_cell);
      evTd.classList.add("bd-ev-cell");
      tr.appendChild(evTd);
      tbody.appendChild(tr);
    }
    el.appendChild(tbody);
    els.tableWrap.appendChild(el);
  }

  function setCollapsed(collapsed, { persist = true } = {}) {
    els.root.classList.toggle("bd-collapsed", collapsed);
    if (persist) browser.storage.local.set({ bdSidebarCollapsed: collapsed });
  }

  function renderShell() {
    const root = document.createElement("div");
    root.id = "bd-sidebar";
    root.innerHTML = `
      <div id="bd-strip" title="Open braindamage sidebar">braindamage</div>
      <div id="bd-panel">
        <div id="bd-header">
          <strong>braindamage</strong>
          <button id="bd-refresh" type="button">Refresh</button>
          <button id="bd-collapse" type="button" title="Collapse">&raquo;</button>
        </div>
        <div id="bd-status"></div>
        <div id="bd-table-wrap"></div>
      </div>
    `;
    document.documentElement.appendChild(root);

    els.root = root;
    els.strip = root.querySelector("#bd-strip");
    els.status = root.querySelector("#bd-status");
    els.tableWrap = root.querySelector("#bd-table-wrap");
    els.refreshBtn = root.querySelector("#bd-refresh");
    els.collapseBtn = root.querySelector("#bd-collapse");

    els.strip.addEventListener("click", () => setCollapsed(false));
    els.collapseBtn.addEventListener("click", () => setCollapsed(true));
    els.refreshBtn.addEventListener("click", () => runFetchAndRender(scrapePage()));

    browser.storage.local.get("bdSidebarCollapsed").then((stored) => {
      setCollapsed(Boolean(stored.bdSidebarCollapsed), { persist: false });
    });
  }

  // --- Fetch + render flow --------------------------------------------------

  async function runFetchAndRender(scraped) {
    if (!scraped.offers.length) {
      setStatus("No listings found on this page yet -- try Refresh once it's finished loading.", "err");
      return;
    }
    if (!scraped.currency) {
      setStatus("Could not detect the page's currency -- refusing to send (this app assumes USD).", "err");
      return;
    }

    setStatus("Working...");
    let reply;
    try {
      reply = await sendScrapeToHost(scraped);
    } catch (e) {
      setStatus(
        "Could not reach the native host: " + e.message + "\n(is it installed? see scripts/install_native_host.sh)",
        "err"
      );
      return;
    }

    if (!reply.ok) {
      setStatus(reply.error, "err");
      return;
    }

    let statusText = `Saved ${reply.written} offer(s) for ${reply.skin_name}.`;
    if (reply.buy_order_written) statusText += " Buy order summary saved.";
    if (reply.table_error) statusText += "\n" + reply.table_error;
    setStatus(statusText, "ok");
    renderTable(reply.table);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // The page is a heavy SPA -- document_idle can fire before its listing
  // rows have actually rendered, so the very first scrape polls for a
  // little while rather than reporting a false "no listings" on every page
  // load. A manual Refresh (after the user scrolls to load more, or
  // changes the wear filter) always scrapes exactly once, immediately.
  async function autoInit() {
    setStatus("Loading...");
    for (let attempt = 0; attempt < 12; attempt++) {
      const scraped = scrapePage();
      if (scraped.offers.length > 0) {
        await runFetchAndRender(scraped);
        return;
      }
      await sleep(1000);
    }
    setStatus("Could not find any listings on this page yet -- try Refresh.", "err");
  }

  renderShell();
  autoInit();

  browser.runtime.onMessage.addListener((message) => {
    if (message && message.type === "toggleSidebar") {
      setCollapsed(!els.root.classList.contains("bd-collapsed"));
    }
  });
})();
