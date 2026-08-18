const NATIVE_HOST = "braindamage_steam_offers";
const LISTING_URL_PATTERN = /^https:\/\/steamcommunity\.com\/market\/listings\/730\//;

// Runs inside the Steam Market page (injected via scripting.executeScript --
// must be fully self-contained, no references to anything outside itself).
// Steam's current listing UI uses hashed/obfuscated CSS module class names
// (e.g. "BPTxiJF58z0-...") that will break on any redeploy, so this
// deliberately selects by label TEXT ("Wear Rating:", "Pattern Template:")
// and DOM structure, never by class name.
//
// Important: this page type lists EVERY wear condition of one weapon
// together (confirmed against a captured page, steam_ex_full.html in the
// repo -- title "Galil AR | Acid Dart - Steam Community Market", with
// Field-Tested/Well-Worn/Battle-Scarred cards all present at once). The URL
// is an opaque route id, not the item's market_hash_name -- do NOT parse it
// for identity. Each card carries its own full "<name> (<wear>)" text
// instead, which is what's actually used here, per card.
//
// Verified once against that capture: float/pattern/price/name+wear all
// live together in one container -- the smallest ancestor of the "Wear
// Rating" label that has BOTH a descendant <button> whose text is exactly
// "Buy" AND a leaf <span> ending in "(<a known wear name>)". If Steam
// changes its markup this selector logic (not just class names) may need
// re-verifying against a live page.
function scrapeListingPage() {
  const WEAR_NAMES = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"];
  const WEAR_SUFFIX_RE = new RegExp("\\((" + WEAR_NAMES.join("|") + ")\\)$");

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
      const hasBuyButton = Array.from(node.querySelectorAll("button")).some(
        (b) => textOf(b) === "Buy"
      );
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
    const buyButton = Array.from(cardRoot.querySelectorAll("button")).find(
      (b) => textOf(b) === "Buy"
    );
    if (!buyButton) return null;
    let sibling = buyButton.previousElementSibling;
    while (sibling && !/[0-9]/.test(textOf(sibling))) {
      sibling = sibling.previousElementSibling;
    }
    return sibling ? textOf(sibling) : null;
  }

  // Buy-order-book summary ("2.302 requests to buy at €143,65 or lower") --
  // Steam only renders this line once a wear filter is active on the page
  // (unfiltered, the page shows all 5 wear cards and no such summary), so
  // finding it is best-effort: search every leaf span's text for the
  // pattern rather than any specific (hashed, unstable) class name, same
  // philosophy as the rest of this scraper. Number formatting follows the
  // page's locale like the price parsing above -- but here in OPPOSITE
  // roles: a EUR-locale page uses '.' as the *count*'s thousands separator
  // and ',' as the *price*'s decimal separator, while a USD-locale page
  // uses ',' for the count's thousands and '.' for the price's decimal.
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
  // "Exterior: <wear>" active-filter chip Steam's Filters UI renders. Same
  // best-effort, text-not-class matching as everything else here; this is
  // the only way to know which wear findBuyOrderSummary's line refers to,
  // since that line itself doesn't repeat the wear name.
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

  // Wallet currency: Steam's classic pages expose g_rgWalletInfo globally;
  // the new React UI may or may not still set it -- unverified against a
  // live page, so this is a best-effort lookup with a currency-symbol
  // fallback (ambiguous: "$" is USD/CAD/AUD/... alike) clearly marked as such.
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

  const SYMBOL_TO_CURRENCY = { "€": "EUR", "$": "USD", "£": "GBP" };

  const wearLabels = Array.from(document.querySelectorAll("div")).filter((div) =>
    textOf(div).startsWith("Wear Rating:")
  );

  const seenRoots = new Set();
  const offers = [];
  let symbolGuess = null;
  let representativeName = null; // any one card's full "<name> (<wear>)" -- used for skin identity

  for (const label of wearLabels) {
    const found = findCard(label);
    if (!found || seenRoots.has(found.root)) continue;
    seenRoots.add(found.root);
    const cardRoot = found.root;

    const wearText = extractLabelValue(cardRoot, "Wear Rating");
    const patternText = extractLabelValue(cardRoot, "Pattern Template");
    const priceText = extractPriceText(cardRoot);
    if (!wearText || !priceText) continue;

    // Wear Rating's decimal separator follows the page's locale (e.g. a
    // European-locale page renders "0,342042387", not "0.342042387") --
    // same normalization as the price parsing below.
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

function setStatus(text, cls) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = cls || "";
}

function fmtMoney(value) {
  if (value === null || value === undefined) return "—"; // em dash
  const sign = value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toFixed(2)}`;
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
  if (cell.color) td.classList.add("c-" + cell.color);
  return td;
}

function renderTable(table) {
  const wrap = document.getElementById("table-wrap");
  wrap.textContent = "";
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
    evTd.classList.add("ev-cell");
    tr.appendChild(evTd);
    tbody.appendChild(tr);
  }
  el.appendChild(tbody);
  wrap.appendChild(el);
}

async function onFetchClick() {
  setStatus("Working...");
  document.getElementById("table-wrap").textContent = "";

  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !LISTING_URL_PATTERN.test(tab.url)) {
    setStatus("Not a Steam Market listing page (steamcommunity.com/market/listings/730/...).", "err");
    return;
  }

  let scraped;
  try {
    const results = await browser.scripting.executeScript({
      target: { tabId: tab.id },
      func: scrapeListingPage,
    });
    scraped = results[0].result;
  } catch (e) {
    setStatus("Failed to scrape the page: " + e.message, "err");
    return;
  }

  if (!scraped.offers.length) {
    setStatus("No listings found on the page -- has it finished loading?", "err");
    return;
  }
  if (!scraped.currency) {
    setStatus("Could not detect the page's currency -- refusing to send (this app assumes USD).", "err");
    return;
  }

  let reply;
  try {
    reply = await browser.runtime.sendNativeMessage(NATIVE_HOST, scraped);
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

document.getElementById("fetch-btn").addEventListener("click", onFetchClick);
