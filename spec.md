let's improve the browser addon popup window. 

The goal is to in the window have a table w/ information pertaining to the mono trade relevant to the skin page currently open.

Each cell should be pricing info.
Each row should be a Wear Tier.

First column should be the skin we have currently open, and in each cell the $ price of actually buying 10 skins of this tier, based on the last offer price info we have on disk. If impossible (less than 10 offers on disk) use "—".
Color code the background of the text in the cells: Purple=fetch date of the oldest offer used in this calculation is less than 1h old, green=less than a day old, orange=older.

The next columns should each contain the per-wear tier info per possible outcome skin, using latest pricing info on disk, w/ the same color coding. 
In the last column, in fat, show the EV of doing a mono trade for this tradeup in the relevant wear condition. Note that we simplify to simply take th e pricing info for skins for this wear category x 10 and outcome skin price info in this wear category (and then steam tax), ignoring the possibilities of mixing different wear buckets and jumping outcome wear buckets in this graphic.

Make each skin name clickable (opens the steam community page for this skin in a new tab).
Make the popup larger instead of cramming info too hard or adding scrollbars unless needed.
Do not add cute overexplaining microcopy, I know what my own app does.

When scraping info from the page, from now on also look for the summary of buy orders for the current skin.
It looks like this:

```
<span class="h9k0m4mdzeY- IokSIloSPlA-" style="--text-weight: var(--font-weight-medium); --text-color: var(--color-text-body-body);">2.302 requests to buy at €143,65 or lower&nbsp;</span> 
```

This info ONLY shows up when a wear bucket is filtered. Thus:
a) do not panic when it isn't there
b) make sure to save it on disk (both price transferred to USD and nr of contracts, may be useful later) in the typical typed and timestamped JSON fashion, with the wear bucket info included.

This buy order info is the best possible OUTCOME skin info we can get, use it in the table for all outcome skins when it exists for this skin x wear. Only if no such info exists, fallback to other pricing info and color its background grey (no matter the age).
