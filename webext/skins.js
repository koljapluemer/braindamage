(async function () {
  const status = document.getElementById("status");
  const container = document.getElementById("collections");

  function money(value) {
    if (value === null || value === undefined) return "—";
    return `${value < 0 ? "-" : ""}$${Math.abs(value).toFixed(2)}`;
  }

  function colorClass(color) {
    return color ? `c-${color}` : "";
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderSkinRow(skin) {
    const row = el("div", "skin-row");
    row.append(el("span", "skin-name", skin.skin_name));

    const sLetter = el("a", `letter ${colorClass(skin.steam_snapshot_color)}`, "S");
    sLetter.href = skin.steam_url;
    sLetter.target = "_blank";
    sLetter.rel = "noopener noreferrer";
    sLetter.title = "Steam order snapshot";
    row.append(sLetter);

    const cLetter = el("span", `letter ${colorClass(skin.csfloat_snapshot_color)}`, "C");
    cLetter.title = "CSfloat order snapshot";
    row.append(cLetter);

    const wears = el("span", "wears");
    for (const wear of skin.wears) {
      const cell = el("span", `wear ${colorClass(wear.color)}`, money(wear.value));
      cell.title = wear.wear_name;
      wears.append(cell);
    }
    row.append(wears);

    row.append(el("span", "sep", "|"));

    const avgs = el("span", "avgs");
    const avgBs = el("span", `avg${skin.group_avg_bs === null ? " empty" : ""}`, money(skin.group_avg_bs));
    avgBs.title = "Mono-trade group avg sell price, Battle-Scarred";
    const avgFn = el("span", `avg${skin.group_avg_fn === null ? " empty" : ""}`, money(skin.group_avg_fn));
    avgFn.title = "Mono-trade group avg sell price, Factory New";
    avgs.append(avgBs, avgFn);
    row.append(avgs);

    return row;
  }

  try {
    const response = await browser.runtime.sendMessage({
      type: "fetchSkinsOverview",
      payload: { action: "skins_overview" },
    });
    if (!response.ok) throw new Error(response.error);
    if (!response.reply.ok) throw new Error(response.reply.error);

    for (const collection of response.reply.collections) {
      const section = el("section", "collection");
      section.append(el("h2", null, collection.collection_name));
      for (const rarity of collection.rarities) {
        const rarityDiv = el("div", "rarity");
        rarityDiv.append(el("h3", null, rarity.rarity_name));
        for (const skin of rarity.skins) {
          rarityDiv.append(
            renderSkinRow({
              ...skin,
              group_avg_bs: rarity.avg_bs,
              group_avg_fn: rarity.avg_fn,
            })
          );
        }
        section.append(rarityDiv);
      }
      container.append(section);
    }

    status.hidden = true;
    container.hidden = false;
  } catch (error) {
    status.textContent = `Could not load skins: ${error.message}`;
  }
})();
