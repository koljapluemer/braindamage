(async function () {
  const status = document.getElementById("status");
  const container = document.getElementById("trades");
  const progress = document.getElementById("progress");
  const progressFill = document.getElementById("progress-fill");
  const rarities = ["Consumer Grade", "Industrial Grade", "Mil-Spec Grade", "Restricted", "Classified"];

  function money(value) {
    if (value === null || value === undefined) return "—";
    return `${value < 0 ? "-" : ""}$${Math.abs(value).toFixed(2)}`;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  try {
    // The sidebar's market dropdown (webext/sidebar.js) persists its choice
    // to browser.storage.local under "bdInputSource" -- this tab has no
    // dropdown of its own, so it just reads whatever was last selected there
    // (defaulting to "steam", same default the native host itself falls
    // back to for an unrecognized/missing value).
    const storedInputSource = await browser.storage.local.get("bdInputSource");
    const inputSource = storedInputSource.bdInputSource === "csfloat" ? "csfloat" : "steam";

    const batches = rarities.flatMap((rarity_name) => [false, true].map((stattrak) => ({ rarity_name, stattrak })));
    const trades = [];
    for (let i = 0; i < batches.length; i++) {
      const batch = batches[i];
      status.textContent = `Calculating ${batch.stattrak ? "StatTrak™ " : ""}${batch.rarity_name} trades (${inputSource}) — ${i}/${batches.length}`;
      const response = await browser.runtime.sendMessage({
        type: "fetchOverview",
        payload: { action: "overview_chunk", input_source: inputSource, ...batch },
      });
      if (!response.ok) throw new Error(response.error);
      if (!response.reply.ok) throw new Error(response.reply.error);
      trades.push(...response.reply.trades);
      progress.setAttribute("aria-valuenow", String(i + 1));
      progressFill.style.width = `${((i + 1) / batches.length) * 100}%`;
    }
    trades.sort((a, b) => a.collection_name.localeCompare(b.collection_name)
      || a.rarity_name.localeCompare(b.rarity_name) || Number(a.stattrak) - Number(b.stattrak));

    for (const trade of trades) {
      const row = el("div", `trade${trade.expected_value > 0 ? " positive" : ""}`);
      row.append(el("span", "collection", trade.collection_name));
      row.append(el("span", "variant", `${trade.stattrak ? "StatTrak™ " : ""}${trade.rarity_name}`));
      row.append(el("span", `ev${trade.ev_source === "naive" ? " naive" : ""}`, money(trade.expected_value)));
      const skins = el("span", "skins");
      for (const skin of trade.input_skins) {
        const link = el("a", skin.price_emphasis === "cheapest" ? "cheapest" : skin.price_emphasis === "same_range" ? "same-range" : "", skin.skin_name.length > 12 ? skin.skin_name.slice(0, 12) + "…" : skin.skin_name);
        link.href = skin.steam_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = skin.skin_name;
        skins.append(link);
      }
      row.append(skins);
      container.append(row);
    }
    status.hidden = true;
    progress.hidden = true;
    container.hidden = false;
  } catch (error) {
    status.textContent = `Could not load overview: ${error.message}`;
  }
})();
