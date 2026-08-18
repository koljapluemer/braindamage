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

  return {
    market_hash_name: representativeName,
    currency,
    currency_source: currencySource,
    offers,
  };
}

function setStatus(text, cls) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = cls || "";
}

async function onFetchClick() {
  setStatus("Working...");

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

  if (reply.ok) {
    setStatus(`Saved ${reply.written} offer(s) for ${reply.skin_name}.`, "ok");
  } else {
    setStatus(reply.error, "err");
  }
}

document.getElementById("fetch-btn").addEventListener("click", onFetchClick);
