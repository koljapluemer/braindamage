// Content script: injected automatically (see manifest.json content_scripts,
// which loads vendor/vue.runtime.global.prod.js, vendor/tailwind.js, and
// sidebar.render.generated.js before this file, so the `Vue`, `tailwind`,
// and `window.__bdSidebarRender` globals below are all ready) on every
// Steam Community Market listing page, docks a collapsible sidebar to the
// right edge, and keeps it stocked with the mono-trade price table for
// whichever skin that page is showing -- no popup click needed.
//
// Runs in an isolated world (Firefox content-script convention), so nothing
// here can see or be seen by the page's own JS -- but browser.runtime
// APIs are still limited compared to a background/extension page, notably
// no sendNativeMessage, so every fetch is relayed through background.js.
//
// SidebarApp's markup lives in sidebar.template.html, not inline here as a
// `template:` string -- see that file's header comment for why (short
// version: Steam's page CSP blocks the eval Vue's runtime compiler needs,
// so the template is precompiled offline into sidebar.render.generated.js).
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
      const text = textOf(span).replace(/ /g, " ");
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

  // The clickable wear-tier filter tabs/buttons Steam renders on a listing
  // page (plain wear-name text, e.g. a bare "Battle-Scarred" -- distinct
  // from findActiveWearFilter's "Exterior: Battle-Scarred" chip, which
  // reflects whichever one is *currently* selected). A listing card's own
  // name span never matches on its own -- it's always the full weapon name
  // plus " (Battle-Scarred)", never a leaf element whose ENTIRE text is
  // just the bare wear name -- so exact-text leaf matching is safe here the
  // same way it is elsewhere in this file (see the header comment: text/
  // DOM-structure matching instead of relying on Steam's hashed class
  // names). Returns { "Factory New": Element, ... } for whichever of the 5
  // it actually finds -- callers must check all 5 are present before
  // relying on this to drive Random Fetch's wear cycling.
  function findWearFilterButtons() {
    const found = {};
    const candidates = document.querySelectorAll("span, div, a, button");
    for (const el of candidates) {
      if (el.children.length > 0) continue;
      const text = textOf(el);
      if (WEAR_NAMES.includes(text) && !found[text]) {
        found[text] = el;
      }
    }
    return found;
  }

  // Steam's own empty-state message for a wear filter with zero current
  // listings -- a leaf element whose exact text is "No Listings Found",
  // same exact-text-leaf matching as findWearFilterButtons above.
  function findNoListingsMessage() {
    const candidates = document.querySelectorAll("div, span, p");
    for (const el of candidates) {
      if (el.children.length === 0 && textOf(el) === "No Listings Found") return el;
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

  // Card DOM elements for whatever scrapePage() last found, keyed by
  // float_value -- lets the "Construct Contract" widget below map the
  // combo's chosen offers back to the actual listing cards on the page, to
  // highlight and scroll to them. Rebuilt fresh on every scrape (including
  // the one "Construct Contract" itself triggers), so it always reflects
  // what's in the window right now, never a stale prior scrape.
  let lastScrapeCardsByFloat = new Map();

  function scrapePage() {
    const wearLabels = Array.from(document.querySelectorAll("div")).filter((div) =>
      textOf(div).startsWith("Wear Rating:")
    );

    lastScrapeCardsByFloat = new Map();
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

      lastScrapeCardsByFloat.set(floatValue, cardRoot);
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

  // Resolves as soon as `wearName`'s own listings have actually rendered
  // after a filter click -- either real offer cards for that wear, or
  // Steam's own "No Listings Found" empty state, whichever the page
  // produces. Driven by a MutationObserver reacting to the real DOM
  // change, not a guessed delay: a fixed sleep before scraping can't tell
  // "still loading" from "done", which is exactly why the previous version
  // of this sometimes captured stale cards and sometimes didn't. Resolves
  // to the settled scrape, or null if neither ever shows up within
  // `timeoutMs` (a dead page shouldn't hang Random Fetch forever).
  //
  // Requires the "Exterior: <wear>" chip (findActiveWearFilter) to also
  // already be showing `wearName`, not just offers/no-listings on their
  // own -- two back-to-back empty wears would otherwise leave "No Listings
  // Found" sitting on screen completely unchanged across the click, which
  // is a real DOM state but a STALE one held over from the previous wear,
  // not proof this wear's own (also-empty) result has loaded yet. The chip
  // text is guaranteed to actually change even when the listings area
  // doesn't, so requiring both together closes that gap.
  function waitForListingsSettled(wearName, timeoutMs = 8000) {
    const settled = () => {
      if (findActiveWearFilter() !== wearName) return null;
      const scraped = scrapePage();
      return scraped.offers.some((o) => o.wear_name === wearName) || findNoListingsMessage() ? scraped : null;
    };

    const already = settled();
    if (already) return Promise.resolve(already);

    return new Promise((resolve) => {
      const observer = new MutationObserver(() => {
        const result = settled();
        if (!result) return;
        observer.disconnect();
        clearTimeout(timer);
        resolve(result);
      });
      const timer = setTimeout(() => {
        observer.disconnect();
        resolve(null);
      }, timeoutMs);
      observer.observe(document.body, { childList: true, subtree: true });
    });
  }

  // --- Native host relay (via background.js -- see its own comment) ------

  async function sendScrapeToHost(payload) {
    const response = await browser.runtime.sendMessage({ type: "fetchOffers", payload });
    if (!response.ok) throw new Error(response.error);
    return response.reply;
  }

  async function sendConstructContractToHost(payload) {
    const response = await browser.runtime.sendMessage({
      type: "constructContract",
      payload: Object.assign({}, payload, { action: "construct_contract" }),
    });
    if (!response.ok) throw new Error(response.error);
    return response.reply;
  }

  // --- "Random Fetch": continuously walk random skins, triggering the
  // standard offer fetch once per wear tier for each. Wear cycling is
  // in-page clicks on Steam's own wear-filter tabs (findWearFilterButtons
  // above) -- NOT a URL parameter and NOT a page load.
  //
  // Steam's listing page only ever shows one page's worth of listings
  // (Steam's own page size, currently ~20), sorted cheapest-first --
  // unfiltered, that's the ~20 cheapest offers across ALL wears mixed
  // together. Clicking a wear tab doesn't just add a buy-order-summary line
  // (see findBuyOrderSummary's own comment for that part) -- it replaces
  // the visible listing cards with THAT wear's own cheapest ~20, which is
  // mostly-to-entirely different data from the unfiltered view and from
  // every other wear's tab. So all 5 tabs each need their own scrapePage()
  // call to actually collect 5 non-redundant pages of offers, not one
  // shared view re-read 5 times -- see runRandomFetchSkin below, which
  // waits for the listing cards themselves (not just Steam's filter
  // indicator) to confirm each tab's own data has actually loaded before
  // scraping it. Clicking through the 5 tabs on one already-loaded page
  // still means 5x the *offer coverage* without 5x the Steam *traffic* a
  // real navigation per wear would cost.
  //
  // A new *skin* genuinely needs a page load (Steam's market isn't a
  // same-page SPA between different market_hash_names), so only that step
  // needs state to survive the reload -- persisted to browser.storage.local
  // under "bdRandomFetch" and picked up again by the freshly-injected
  // content script on the next page. --------------------------------------

  const RANDOM_FETCH_STORAGE_KEY = "bdRandomFetch";
  const RANDOM_FETCH_IDLE = { active: false, skinName: null, baseUrl: null };
  // Randomized pause before every Steam page load Random Fetch triggers
  // (never a fixed cadence) -- an automated loop that hits Steam back-to-back
  // as fast as pages render looks like a bot and risks rate-limiting/account
  // scrutiny; this is deliberately not tunable from the UI.
  const RANDOM_FETCH_MIN_DELAY_MS = 3000;
  const RANDOM_FETCH_MAX_DELAY_MS = 9000;

  function randomDelayMs() {
    return RANDOM_FETCH_MIN_DELAY_MS + Math.random() * (RANDOM_FETCH_MAX_DELAY_MS - RANDOM_FETCH_MIN_DELAY_MS);
  }

  // Picks a random skin from the local catalog DB via the native host, NOT
  // from Steam itself -- Random Fetch already hammers Steam with page
  // loads; it must not also hit Steam's own search API just to decide
  // where to go next. `baseUrl` is a *working* Steam listing URL for the
  // skin (braindamage.steam_offers_host reuses the same
  // mono_trade_table._steam_listing_url this app already uses for every
  // other Steam link it shows) -- Steam 404s on a bare skin name with no
  // wear suffix at all, so this must not be reconstructed from the name
  // here. No wear is ever layered onto this URL -- see the block comment
  // above for why wear cycling doesn't touch the URL at all any more.
  async function pickRandomSkin() {
    const response = await browser.runtime.sendMessage({
      type: "randomSkin",
      payload: { action: "random_skin" },
    });
    if (!response.ok) throw new Error(response.error);
    if (!response.reply.ok) throw new Error(response.reply.error);
    const skinName = response.reply.skin_name;
    const baseUrl = response.reply.steam_url;
    // Hard requirement, not an assumption: this app has previously navigated
    // to a literal "https://.../undefined?..." URL because sidebar.js and
    // the native host's reply shape drifted out of sync (Firefox doesn't
    // hot-reload content scripts, so an old sidebar.js kept running against
    // a newer host reply and silently read a field that no longer existed).
    // Never again -- any reply that isn't exactly the two strings we need
    // is treated as a hard failure, not "undefined" quietly flowing into a
    // URL that gets sent to Steam.
    if (typeof skinName !== "string" || !skinName || typeof baseUrl !== "string" || !baseUrl) {
      throw new Error(
        "native host reply is missing skin_name/steam_url -- reload the extension, it's probably out of date"
      );
    }
    return { skinName, baseUrl };
  }

  // --- Tailwind setup --------------------------------------------------
  // Scope every generated utility rule under "#bd-sidebar" (with
  // !important) so it can never bleed onto Steam's own page, and disable
  // Preflight so we don't reset margins/borders/etc. document-wide -- the
  // sidebar relies on sidebar.css's `#bd-sidebar { all: initial }` for its
  // own isolation from the page instead.
  tailwind.config = {
    important: "#bd-sidebar",
    corePlugins: { preflight: false },
  };

  // --- Tiny hand-rolled state machine -----------------------------------
  // Each machine is just {state, send, is}: `state` is a Vue ref holding
  // the current state name, `send(event)` looks up transitions[state][event]
  // and moves there (or is a no-op + console warning if that event isn't
  // valid from the current state), `is(...states)` is a template-friendly
  // membership check. No external FSM library -- this is the whole thing.
  function createMachine(name, transitions, initial) {
    const state = Vue.ref(initial);
    function send(event) {
      const next = transitions[state.value] && transitions[state.value][event];
      if (!next) {
        console.warn(`[bd-sidebar] ${name}: ignored "${event}" while in "${state.value}"`);
        return false;
      }
      state.value = next;
      return true;
    }
    function is(...states) {
      return states.includes(state.value);
    }
    return { state, send, is };
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function fmtMoney(value) {
    if (value === null || value === undefined) return "—"; // em dash
    const sign = value < 0 ? "-" : "";
    return `${sign}$${Math.abs(value).toFixed(2)}`;
  }

  function fmtPct(value) {
    if (value === null || value === undefined) return "—";
    return `${(value * 100).toFixed(1)}%`;
  }

  function fmtDate(iso) {
    // generated_at is a naive-UTC ISO string ("YYYY-MM-DDTHH:MM:SS...") --
    // slice mm-dd out directly rather than going through Date(), which would
    // reinterpret a timezone-less string in the browser's local time.
    return iso.slice(5, 7) + "-" + iso.slice(8, 10);
  }

  function priceCellClass(cell) {
    if (!cell || !cell.color) return "";
    return (
      {
        purple: "bg-purple-500/40",
        green: "bg-green-500/40",
        orange: "bg-orange-500/40",
        grey: "bg-gray-400/40",
      }[cell.color] || ""
    );
  }

  const BTN_CLASS =
    "bg-[#2a475e] text-[#c7d5e0] border border-white/20 rounded-sm px-2.5 py-1 text-[11px] cursor-pointer hover:bg-[#375875] disabled:opacity-50 disabled:cursor-default";
  const TABLE_CLASS = "border-collapse w-full";
  const TH_CLASS = "border border-white/10 px-2.5 py-1 text-center bg-[#1b2838] whitespace-nowrap";
  const TD_CLASS = "border border-white/10 px-2.5 py-1 text-right whitespace-nowrap";

  // --- Root Vue component ------------------------------------------------

  const SidebarApp = {
    setup() {
      const { ref, watch, onMounted, nextTick } = Vue;

      // -- "Random Fetch" state, persisted across page loads (see the
      // top-of-file comment for why) -------------------------------------
      const randomFetch = ref(RANDOM_FETCH_IDLE);
      // Which wear tab Random Fetch is currently clicked into, purely for
      // the floating Stop button's label -- NOT persisted (unlike
      // randomFetch above, it never needs to survive a reload: wear cycling
      // never navigates, see the "Random Fetch" block comment).
      const randomFetchWear = ref("");

      // -- collapsed/expanded, persisted across page loads --------------
      const sidebar = createMachine(
        "sidebar",
        {
          collapsed: { toggle: "expanded" },
          expanded: { toggle: "collapsed" },
        },
        "expanded"
      );
      browser.storage.local.get("bdSidebarCollapsed").then((stored) => {
        sidebar.state.value = stored.bdSidebarCollapsed ? "collapsed" : "expanded";
      });
      watch(sidebar.state, (value) => {
        // While Random Fetch is running the sidebar is forced collapsed
        // (see the watcher below) -- don't let that clobber the user's
        // real preference, so it's restored as-is once they hit Stop.
        if (randomFetch.value.active) return;
        browser.storage.local.set({ bdSidebarCollapsed: value === "collapsed" });
      });
      // Force (and keep) the sidebar collapsed for as long as Random Fetch
      // is active, regardless of the persisted preference above or of
      // which page load this is.
      watch(
        randomFetch,
        (value) => {
          if (value.active) sidebar.state.value = "collapsed";
        },
        { deep: true }
      );

      // -- main price table ------------------------------------------------
      const fetchFsm = createMachine(
        "fetch",
        {
          idle: { start: "loading" },
          loading: { succeed: "ready", fail: "error" },
          ready: { start: "loading" },
          error: { start: "loading" },
        },
        "idle"
      );
      const fetchStatus = ref("");
      const table = ref(null);
      // Last 5 "Construct Contract" results for whichever skin the page is
      // currently showing -- refreshed by both the always-on fetch and
      // Construct Contract itself, since either one resolves the page's
      // skin and the host returns this alongside its own reply.
      const contractHistory = ref([]);

      // -- float diagrams: price-vs-float, input-float-vs-output-revenue,
      // and EV-vs-float charts for the skin the page is currently showing
      // (see webext/float_diagrams.js and mono_trade_table.build_float_diagram_data).
      // floatDiagrams holds the raw reply; the canvases below are the DOM
      // refs Chart.js draws into once floatDiagrams becomes non-null (see
      // the watcher after onMounted). Chart.js instances themselves are
      // deliberately NOT reactive state -- kept in `floatCharts` (plain
      // closure variable, not a ref) so Vue never tries to proxy them.
      const floatDiagrams = ref(null);
      const bucketCanvas = ref(null);
      const revenueCanvas = ref(null);
      const evCanvas = ref(null);
      const topRanges = ref([]);
      let floatCharts = null;

      async function runFetchAndRender(scraped) {
        fetchFsm.send("start");
        if (!scraped.offers.length) {
          fetchStatus.value = "No listings found on this page yet -- try Refresh once it's finished loading.";
          fetchFsm.send("fail");
          return;
        }
        if (!scraped.currency) {
          fetchStatus.value = "Could not detect the page's currency -- refusing to send (this app assumes USD).";
          fetchFsm.send("fail");
          return;
        }

        fetchStatus.value = "Working...";
        let reply;
        try {
          reply = await sendScrapeToHost(scraped);
        } catch (e) {
          fetchStatus.value =
            "Could not reach the native host: " + e.message + "\n(is it installed? see scripts/install_native_host.sh)";
          fetchFsm.send("fail");
          return;
        }

        if (!reply.ok) {
          fetchStatus.value = reply.error;
          fetchFsm.send("fail");
          return;
        }

        let statusText = `Saved ${reply.written} offer(s) for ${reply.skin_name}.`;
        if (reply.buy_order_written) statusText += " Buy order summary saved.";
        if (reply.table_error) statusText += "\n" + reply.table_error;
        fetchStatus.value = statusText;
        table.value = reply.table;
        contractHistory.value = reply.contract_history || [];
        floatDiagrams.value = reply.float_diagrams || null;
        fetchFsm.send("succeed");
      }

      // The page is a heavy SPA -- document_idle can fire before its listing
      // rows have actually rendered, so the very first scrape polls for a
      // little while rather than reporting a false "no listings" on every
      // page load. A manual Refresh (after the user scrolls to load more,
      // or changes the wear filter) always scrapes exactly once, immediately.
      async function autoInit() {
        fetchStatus.value = "Loading...";
        for (let attempt = 0; attempt < 12; attempt++) {
          const scraped = scrapePage();
          if (scraped.offers.length > 0) {
            await runFetchAndRender(scraped);
            return;
          }
          await sleep(1000);
        }
        fetchFsm.send("start");
        fetchStatus.value = "Could not find any listings on this page yet -- try Refresh.";
        fetchFsm.send("fail");
      }

      // -- "Random Fetch": cycle every wear tab on the current skin's page
      // (in-page clicks, no navigation), then move to a new random skin
      // (a real page load) and repeat. See the top-of-file block comment
      // for why wear cycling and skin cycling work so differently. --------

      async function isRandomFetchStillActive() {
        // Always re-read from storage rather than trusting the in-memory
        // ref -- the only reliable way to notice Stop was clicked while an
        // await (a click's settle-poll, runFetchAndRender, the pacing
        // pause, ...) was in flight.
        const stored = await browser.storage.local.get(RANDOM_FETCH_STORAGE_KEY);
        const current = stored[RANDOM_FETCH_STORAGE_KEY];
        return !!(current && current.active);
      }

      async function stopRandomFetch() {
        randomFetch.value = RANDOM_FETCH_IDLE;
        randomFetchWear.value = "";
        await browser.storage.local.set({ [RANDOM_FETCH_STORAGE_KEY]: RANDOM_FETCH_IDLE });
        // Random Fetch's forced-collapse watcher only ever drives the
        // sidebar *into* "collapsed" -- restore whatever the user's real
        // preference was, now that it's safe to persist again.
        const stored = await browser.storage.local.get("bdSidebarCollapsed");
        sidebar.state.value = stored.bdSidebarCollapsed ? "collapsed" : "expanded";
      }

      // Persists `picked` as the active skin and navigates to it -- the
      // only place Random Fetch ever touches window.location.href, and the
      // only place it ever needs the randomized inter-request pause (wear
      // cycling on an already-loaded page doesn't touch Steam at all).
      async function goToRandomFetchSkin(picked) {
        const state = { active: true, skinName: picked.skinName, baseUrl: picked.baseUrl };
        randomFetch.value = state;
        randomFetchWear.value = "";
        await browser.storage.local.set({ [RANDOM_FETCH_STORAGE_KEY]: state });

        // Randomized pause before touching Steam again (see
        // RANDOM_FETCH_MIN/MAX_DELAY_MS) -- re-check storage afterwards in
        // case Stop was clicked during the wait, so a click during the
        // pause actually prevents the next page load instead of only
        // taking effect after it.
        fetchStatus.value = "Random Fetch: pausing before the next page...";
        await sleep(randomDelayMs());
        if (!(await isRandomFetchStillActive())) return;

        // Last line of defense: never call window.location.href (i.e.
        // never send Steam a request) without a URL that's actually been
        // validated. picked.baseUrl already went through pickRandomSkin's
        // validation, but checking again here -- right at the point that
        // actually contacts Steam -- means a future refactor that moves a
        // bad URL past that first check still can't slip through.
        if (typeof state.baseUrl !== "string" || !state.baseUrl) {
          fetchStatus.value = "Random Fetch: refusing to navigate (no valid Steam URL), stopping.";
          await stopRandomFetch();
          return;
        }
        window.location.href = state.baseUrl;
      }

      async function startRandomFetch() {
        fetchStatus.value = "Random Fetch: picking a random skin...";
        let picked;
        try {
          picked = await pickRandomSkin();
        } catch (e) {
          fetchStatus.value = "Random Fetch: could not pick a skin (" + e.message + ").";
          return;
        }
        await goToRandomFetchSkin(picked);
      }

      // Called once this page's skin has been through every wear tab --
      // picks a fresh random skin and navigates to it. Keeps going even if
      // the fetch for some individual wear tab failed or found nothing
      // (see runRandomFetchSkin below); a single bad skin shouldn't stall
      // the loop, only a broken skin *picker* should (handled below by
      // stopping outright).
      async function advanceRandomFetchSkin() {
        if (!(await isRandomFetchStillActive())) return;
        let picked;
        try {
          picked = await pickRandomSkin();
        } catch (e) {
          fetchStatus.value = "Random Fetch: could not pick the next skin (" + e.message + "), stopping.";
          await stopRandomFetch();
          return;
        }
        await goToRandomFetchSkin(picked);
      }

      // Drives the current page: waits for Steam's wear-filter tabs (and a
      // first real listing) to render, then for each of the 5 wears in
      // turn -- click its tab, wait for that wear's listings to actually
      // settle (see waitForListingsSettled), then run the standard offer
      // fetch. No navigation anywhere in this function -- see the "Random
      // Fetch" block comment up top for why wear cycling never leaves the
      // page.
      async function runRandomFetchSkin() {
        // Wait for the wear filter tabs AND at least one real listing to be
        // on screen -- not just the tabs -- before clicking anything. The
        // first click landing on a page that's only partially finished
        // loading (tabs rendered, but the rest of the SPA not yet
        // interactive) is exactly what made the very first wear tab
        // unreliable before this wait existed.
        fetchStatus.value = "Random Fetch: waiting for the page to finish loading...";
        let buttons = {};
        for (let attempt = 0; attempt < 12; attempt++) {
          buttons = findWearFilterButtons();
          if (WEAR_NAMES.every((w) => buttons[w]) && scrapePage().offers.length > 0) break;
          await sleep(1000);
        }

        if (!WEAR_NAMES.every((w) => buttons[w])) {
          // Not every skin necessarily renders wear tabs the same way --
          // don't stall the whole loop over one page; just take whatever's
          // on screen once, same as the always-on fetch does, and move on.
          fetchStatus.value = "Random Fetch: no wear filter tabs found on this page -- fetching as-is.";
          await runFetchAndRender(scrapePage());
          await advanceRandomFetchSkin();
          return;
        }

        // The very first filter click on a freshly-loaded page is
        // unreliable even after the wait above (tabs + a first listing
        // being on screen isn't the same as the page's filter-click
        // handling actually being ready) -- one extra fixed pause here,
        // on top of everything already waited for, specifically before
        // that first click.
        await sleep(3000);

        for (const wearName of WEAR_NAMES) {
          if (!(await isRandomFetchStillActive())) return;
          await attemptWearFetch(wearName, buttons);
        }

        await advanceRandomFetchSkin();
      }

      // Clicks one wear tab and waits for its listings to actually settle
      // (waitForListingsSettled: real offer cards for this wear, or
      // Steam's "No Listings Found" -- either is a definitive, awaited
      // signal, never a guessed delay), retrying the click once if
      // settling times out, then runs the standard offer fetch.
      async function attemptWearFetch(wearName, buttons) {
        randomFetchWear.value = wearName;
        fetchStatus.value = `Random Fetch: switching to ${wearName}...`;
        buttons[wearName].click();

        let scraped = await waitForListingsSettled(wearName);
        if (!scraped) {
          fetchStatus.value = `Random Fetch: ${wearName} didn't settle -- retrying the click...`;
          buttons[wearName].click();
          scraped = await waitForListingsSettled(wearName);
        }

        if (!(await isRandomFetchStillActive())) return;

        if (!scraped) {
          // Genuine failure to settle, not a zero-listings case -- a
          // skin's "No Listings Found" IS a settled result, handled above
          // -- so skip just this one wear rather than stalling the skin.
          fetchStatus.value = `Random Fetch: ${wearName} tab never settled -- skipping it.`;
          return;
        }

        await runFetchAndRender(scraped);
      }

      // -- "Construct Contract" widget: the single best mono trade-up combo
      // buildable from exactly what scrapePage() just found in the browser
      // window, plus highlighting/scrolling to the chosen listings on the
      // page itself (see lastScrapeCardsByFloat above). --------------------
      const contractFsm = createMachine(
        "contract",
        {
          idle: { start: "loading" },
          loading: { succeed: "ready", fail: "error" },
          ready: { start: "loading" },
          error: { start: "loading" },
        },
        "idle"
      );
      const contractStatus = ref("");
      const contract = ref(null);
      const contractOffers = ref([]); // [{offer, element: Element | null}, ...]
      const contractIndex = ref(-1);

      function clearContractHighlights() {
        for (const { element } of contractOffers.value) {
          if (element) element.classList.remove("bd-highlight-card", "bd-highlight-current");
        }
      }

      function focusContractOffer(index) {
        if (!contractOffers.value.length) return;
        const n = contractOffers.value.length;
        contractIndex.value = ((index % n) + n) % n;
        for (const { element } of contractOffers.value) {
          if (element) element.classList.remove("bd-highlight-current");
        }
        const current = contractOffers.value[contractIndex.value];
        if (current.element) {
          current.element.classList.add("bd-highlight-current");
          current.element.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }

      async function runConstructContract(scraped) {
        contractFsm.send("start");
        clearContractHighlights();
        contractOffers.value = [];
        contractIndex.value = -1;
        contract.value = null;

        if (!scraped.offers.length) {
          contractStatus.value = "No listings found on this page yet -- try Refresh once it's finished loading.";
          contractFsm.send("fail");
          return;
        }
        if (!scraped.currency) {
          contractStatus.value = "Could not detect the page's currency -- refusing to send (this app assumes USD).";
          contractFsm.send("fail");
          return;
        }

        contractStatus.value = "Constructing contract from what's on this page right now...";
        try {
          const reply = await sendConstructContractToHost(scraped);
          if (!reply.ok) {
            contractStatus.value = reply.error;
            contractFsm.send("fail");
            return;
          }
          contract.value = reply.contract;
          contractHistory.value = reply.contract_history || [];
          // Match each chosen offer back to its card on the page (by
          // float_value, the same synthetic identity
          // braindamage.steam_offer_combos uses -- see scrapePage above)
          // and highlight it.
          contractOffers.value = reply.contract.offers.map((offer) => ({
            offer,
            element: lastScrapeCardsByFloat.get(offer.float_value) || null,
          }));
          for (const { element } of contractOffers.value) {
            if (element) element.classList.add("bd-highlight-card");
          }
          contractFsm.send("succeed");
          await nextTick();
          focusContractOffer(0);
        } catch (e) {
          contractStatus.value = "Could not reach the native host: " + e.message;
          contractFsm.send("fail");
        }
      }

      // Rebuilds the 3 float-diagram charts whenever floatDiagrams changes
      // (a fresh scrape/refresh, or a new skin's page) -- always destroys
      // whatever charts were drawn before, since v-if unmounts/remounts the
      // canvases themselves whenever floatDiagrams flips to/from null (e.g.
      // a skin that isn't a usable trade-up input), which would otherwise
      // leave Chart.js holding a reference to a detached canvas.
      watch(floatDiagrams, async (value) => {
        if (floatCharts) {
          floatCharts.destroy();
          floatCharts = null;
        }
        if (!value) {
          topRanges.value = [];
          return;
        }
        await nextTick();
        if (!bucketCanvas.value || !revenueCanvas.value || !evCanvas.value) return;
        floatCharts = window.__bdFloatDiagrams.render(
          { bucketCanvas: bucketCanvas.value, revenueCanvas: revenueCanvas.value, evCanvas: evCanvas.value },
          value
        );
        topRanges.value = floatCharts.topRanges;
      });

      onMounted(async () => {
        const stored = await browser.storage.local.get(RANDOM_FETCH_STORAGE_KEY);
        const state = stored[RANDOM_FETCH_STORAGE_KEY];
        if (state && state.active) {
          randomFetch.value = state;
          await runRandomFetchSkin();
        } else {
          await autoInit();
        }
      });

      return {
        sidebar,
        openOverview: () => browser.runtime.sendMessage({ type: "openOverview" }),
        toggleSidebar: () => sidebar.send("toggle"),
        randomFetch,
        randomFetchWear,
        startRandomFetch,
        stopRandomFetch,
        fetchFsm,
        fetchStatus,
        table,
        refresh: () => runFetchAndRender(scrapePage()),
        floatDiagrams,
        bucketCanvas,
        revenueCanvas,
        evCanvas,
        topRanges,
        contractFsm,
        contractStatus,
        contract,
        contractOffers,
        contractIndex,
        contractHistory,
        construct: () => runConstructContract(scrapePage()),
        focusContractOffer,
        fmtMoney,
        fmtPct,
        fmtDate,
        priceCellClass,
        btnClass: BTN_CLASS,
        tableClass: TABLE_CLASS,
        thClass: TH_CLASS,
        tdClass: TD_CLASS,
      };
    },

    render: window.__bdSidebarRender,
  };

  // --- Mount ---------------------------------------------------------------

  const container = document.createElement("div");
  container.id = "bd-sidebar";
  document.documentElement.appendChild(container);

  const vm = Vue.createApp(SidebarApp).mount(container);

  browser.runtime.onMessage.addListener((message) => {
    if (message && message.type === "toggleSidebar") {
      vm.toggleSidebar();
    }
  });
})();
