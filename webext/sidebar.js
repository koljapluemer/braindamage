// Content script: injected automatically (see manifest.json content_scripts,
// which loads vendor/vue.global.prod.js and vendor/tailwind.js before this
// file so the `Vue` and `tailwind` globals below are ready) on every Steam
// Community Market listing page, docks a collapsible sidebar to the right
// edge, and keeps it stocked with the mono-trade price table for whichever
// skin that page is showing -- no popup click needed.
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
        browser.storage.local.set({ bdSidebarCollapsed: value === "collapsed" });
      });

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

      onMounted(autoInit);

      return {
        sidebar,
        toggleSidebar: () => sidebar.send("toggle"),
        fetchFsm,
        fetchStatus,
        table,
        refresh: () => runFetchAndRender(scrapePage()),
        contractFsm,
        contractStatus,
        contract,
        contractOffers,
        contractIndex,
        construct: () => runConstructContract(scrapePage()),
        focusContractOffer,
        fmtMoney,
        fmtPct,
        priceCellClass,
        btnClass: BTN_CLASS,
        tableClass: TABLE_CLASS,
        thClass: TH_CLASS,
        tdClass: TD_CLASS,
      };
    },

    template: `
      <div v-show="sidebar.is('collapsed')"
           @click="toggleSidebar"
           title="Open braindamage sidebar"
           class="w-[26px] h-full bg-[#1b2838] text-[#c7d5e0] border-l border-white/15 flex items-center justify-center cursor-pointer select-none tracking-wide py-2.5 hover:bg-[#24374c]"
           style="writing-mode: vertical-rl; text-orientation: mixed;">
        braindamage
      </div>

      <div v-show="sidebar.is('expanded')"
           class="block w-fit max-w-[min(920px,92vw)] max-h-full overflow-auto bg-[#171a21] text-[#c7d5e0] border-l border-white/15 shadow-[-6px_0_16px_rgba(0,0,0,0.45)] p-2.5 px-3">

        <div class="flex items-center gap-2 whitespace-nowrap">
          <strong class="flex-1 text-[#66c0f4] text-[13px]">braindamage</strong>
          <button type="button" :class="btnClass" :disabled="fetchFsm.is('loading')" @click="refresh">Refresh</button>
          <button type="button" :class="btnClass" :disabled="contractFsm.is('loading')" @click="construct"
                  title="Run the buy combo search on just what's visible on this page right now">
            Construct Contract
          </button>
          <button type="button" :class="btnClass" title="Collapse" @click="toggleSidebar">&raquo;</button>
        </div>

        <div class="mt-2 whitespace-pre-wrap text-[11px] min-h-[1em]"
             :class="{ 'text-[#6fd06f]': fetchFsm.is('ready'), 'text-[#ff6b6b]': fetchFsm.is('error') }">
          {{ fetchStatus }}
        </div>

        <div v-if="table" class="mt-2.5 overflow-x-auto">
          <table :class="tableClass">
            <thead>
              <tr>
                <th :class="thClass + ' text-left'">Wear</th>
                <th :class="thClass">
                  <a :href="table.input_header.steam_url" target="_blank" rel="noopener noreferrer" class="text-[#66c0f4] no-underline hover:underline">
                    {{ table.input_header.skin_name }}
                  </a>
                </th>
                <th v-for="header in table.outcome_headers" :key="header.skin_id" :class="thClass">
                  <a :href="header.steam_url" target="_blank" rel="noopener noreferrer" class="text-[#66c0f4] no-underline hover:underline">
                    {{ header.skin_name }}
                  </a>
                </th>
                <th :class="thClass">EV</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in table.rows" :key="row.wear_name">
                <td :class="tdClass + ' text-left'">{{ row.wear_name }}</td>
                <td :class="[tdClass, priceCellClass(row.input_cell)]">{{ fmtMoney(row.input_cell.value) }}</td>
                <td v-for="(cell, i) in row.outcome_cells" :key="i" :class="[tdClass, priceCellClass(cell)]">
                  {{ fmtMoney(cell.value) }}
                </td>
                <td :class="[tdClass, priceCellClass(row.ev_cell), 'font-bold']">{{ fmtMoney(row.ev_cell.value) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="mt-2.5">
          <div v-if="contractFsm.is('idle', 'loading', 'error')" class="whitespace-pre-wrap text-[11px]"
               :class="{ 'text-[#ff6b6b]': contractFsm.is('error') }">
            {{ contractStatus }}
          </div>

          <div v-else-if="contract">
            <div class="flex items-center gap-2 flex-wrap text-[13px]">
              <strong>{{ contract.stattrak ? 'StatTrak™ ' + contract.skin_name : contract.skin_name }}</strong>
              <span class="text-[#8f98a0] font-normal">— {{ contract.collection_name }} [{{ contract.rarity_name }}]</span>
              <span class="ml-auto font-bold" :class="contract.expected_value >= 0 ? 'text-[#6fd06f]' : 'text-[#ff6b6b]'">
                EV {{ fmtMoney(contract.expected_value) }}
              </span>
            </div>

            <div class="flex gap-4 flex-wrap mt-2 mb-1">
              <div class="flex flex-col">
                <span class="text-[#8f98a0] text-[10px]">Real cost (10 listings)</span>
                <span>{{ fmtMoney(contract.real_cost) }}</span>
              </div>
              <div class="flex flex-col">
                <span class="text-[#8f98a0] text-[10px]">Expected output value</span>
                <span>{{ fmtMoney(contract.expected_output_value) }}</span>
              </div>
              <div class="flex flex-col">
                <span class="text-[#8f98a0] text-[10px]">ROI</span>
                <span>{{ contract.roi === null ? '—' : fmtPct(contract.roi) }}</span>
              </div>
              <div class="flex flex-col">
                <span class="text-[#8f98a0] text-[10px]">Avg. normalized float</span>
                <span>{{ contract.avg_float.toFixed(4) }}</span>
              </div>
            </div>

            <div class="flex items-center gap-2 my-2">
              <button type="button" :class="btnClass" @click="focusContractOffer(contractIndex - 1)">&lsaquo; Prev</button>
              <span class="tabular-nums">{{ contractIndex + 1 }} / {{ contractOffers.length }}</span>
              <button type="button" :class="btnClass" @click="focusContractOffer(contractIndex + 1)">Next &rsaquo;</button>
            </div>

            <h3 class="text-[11px] uppercase tracking-wide text-[#8f98a0] mt-3 mb-1">The 10 listings to buy</h3>
            <table :class="tableClass">
              <thead>
                <tr>
                  <th :class="thClass">Wear</th>
                  <th :class="thClass">Float</th>
                  <th :class="thClass">Pattern</th>
                  <th :class="thClass">Price</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(entry, i) in contractOffers" :key="i"
                    class="cursor-pointer hover:bg-[#66c0f4]/15"
                    :class="{ 'opacity-60': !entry.element }"
                    @click="focusContractOffer(i)">
                  <td :class="tdClass">{{ entry.offer.wear_name || '—' }}</td>
                  <td :class="tdClass">{{ entry.offer.float_value.toFixed(6) }}</td>
                  <td :class="tdClass">{{ entry.offer.pattern_seed === null || entry.offer.pattern_seed === undefined ? '—' : entry.offer.pattern_seed }}</td>
                  <td :class="tdClass">{{ fmtMoney(entry.offer.price) }}</td>
                </tr>
              </tbody>
            </table>

            <h3 class="text-[11px] uppercase tracking-wide text-[#8f98a0] mt-3 mb-1">Possible outputs</h3>
            <table :class="tableClass">
              <thead>
                <tr>
                  <th :class="thClass">Output</th>
                  <th :class="thClass">Collection</th>
                  <th :class="thClass">Chance</th>
                  <th :class="thClass">Wear</th>
                  <th :class="thClass">Net sell</th>
                  <th :class="thClass">Contribution</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(outcome, i) in contract.outcomes" :key="i">
                  <td :class="tdClass">{{ outcome.skin_name }}</td>
                  <td :class="tdClass">{{ outcome.collection_name }}</td>
                  <td :class="tdClass">{{ fmtPct(outcome.probability) }}</td>
                  <td :class="tdClass">{{ outcome.predicted_wear }}</td>
                  <td :class="tdClass">{{ fmtMoney(outcome.net_price) }}</td>
                  <td :class="tdClass">{{ fmtMoney(outcome.contribution) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `,
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
