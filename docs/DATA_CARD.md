# CulinaCompass SG — Data Card

**Scope (capped):** one destination (Singapore), one cuisine (South Indian vegetarian home cooking), **15 dishes, 77 ingredients** (61 core + 16 used only as substitutes), 14 utensils.
**Evidence collected:** 2026-09-28 · 286 evidence rows · 9 sources.

## 1. Sources

| Source | Type | What was read | How | Stock verified? |
|---|---|---|---|---|
| Waangoo (waangoo.com) | Online Indian grocer (SG) | Title, price, `available` | Shopify predictive-search JSON | Yes |
| Kiasumart (kiasumart.com) | Online Indian grocer (SG) | Name, price, `is_in_stock` | WooCommerce Store API JSON | Yes |
| Karthika Supermarket (karthika.sg) | Indian supermarket (Buffalo Rd, Yishun) | Appliance category page | HTML page | Yes (page shows stock) |
| Selvi Stores (selvistores.sg) | Small Indian grocer | Search JSON | Shopify | Weak: $0 placeholder prices, no pack sizes |
| Farms2Home.sg | Online Indian grocer | Store API JSON | WooCommerce | Yes (everything checked was out of stock) |
| SGWetMarket | Online wet market | Search JSON | Shopify | Yes |
| NTUC FairPrice | Mainstream supermarket | Product pages found in a search index | Web search | **No.** robots.txt blocks automated fetching, so these rows can only reach *Likely* |
| data.gov.sg / SingStat | Government | Average retail prices, Dec 2024 (latest month in the resource) | Datastore API | n/a (reference prices) |
| SFA (sfa.gov.sg) | Government | Mandatory allergen list for labels | Web page | n/a (dietary rules) |

Recipes: 14 from indianhealthyrecipes.com (Swasthi's Recipes) and 1 from vegrecipesofindia.com (avial). Ingredient lines are kept verbatim in `qty_text`, and the gram conversions are documented per row.

## 2. Availability grading (deterministic, `culinacompass/grading.py`)

| Grade | Rule |
|---|---|
| **Confirmed** | At least one exact-match product read from a retailer catalogue with in-stock = true on the verification date |
| **Likely** | Exact product exists, but stock is not verified: it was out of stock when checked, or seen only via a search index |
| **Difficult** | Only a variant form exists (pickle instead of leaves, powder instead of whole, paste instead of block, seeds to grow), **or** nothing was found after searching at least 3 independent sources |
| **Unknown** | Nothing found and fewer than 3 sources searched |

At read time, a Confirmed grade becomes Likely once its evidence is more than 30 days old (`effective_grade`). Grades are never hand-edited: `validate.py` recomputes every grade from the evidence and fails on any mismatch.

## 3. Results

| | Confirmed | Likely | Difficult | Unknown |
|---|---|---|---|---|
| Core ingredients (61) | 57 | 0 | 4 | 0 |
| Substitute-only (16) | 12 | 3 | 0 | 1 |
| Specialty utensils (11) | 10 | 1 | 0 | 0 |

**Not readily available in Singapore (all evidence-backed):**
- **Gongura leaves:** fresh leaves were not found at any of 7 sources. Only gongura *pickles* (in stock at Waangoo and Kiasumart), dried roselle calyces (FairPrice) and roselle seeds (Horti) turned up. This is the signature ingredient of gongura pachadi.
- **Mangalore cucumber:** absent at 4 sources. Sourced substitutes are ash gourd and pumpkin, both Confirmed.
- **Whole Kashmiri chilli:** only Kashmiri *powder* and Byadgi powder were found.
- **Lentil vadiyalu:** only Tamil rice/onion *vadagam* were found.

**Other findings worth designing for:**
- **Alias collision:** searching "drumstick" at Waangoo, Kiasumart and FairPrice returns chicken drumsticks. Use *moringa* / *murungakkai* / *mulakkada*.
- In Singapore, **"yam" means taro**, not elephant foot yam.
- **Asafoetida was out of stock at Waangoo** (all 8 brands). Only Kiasumart had it in stock.
- **Yellow moong dal** was in stock only as a 5 kg bundle (S$20). This makes ven pongal's empty-pantry cost S$53.
- Imported Indian mixer grinders at Karthika are sold with **"No Local Warranty"**. Singapore uses 230 V with Type G plugs.

## 4. Costs (`dish_costs.csv`)

There are two numbers per dish, because a newcomer's pantry is empty:
- **Pro-rated:** the share of each pack used (S$0.68–7.07 per dish).
- **Cash outlay:** the whole packs you'd have to buy (S$8.96 for idli up to S$62.05 for gutti vankaya, driven mostly by spices).

Ingredients with no in-stock priced listing are listed as unpriced and never estimated.

## 5. Verification log

- **Spot check (2026-09-28):** 12 listings were drawn at random with a fixed seed and re-fetched from product endpoints. **11 of 11 fetched rows matched** title, price and stock. One fetch failed (a server error), and one extra check confirmed Waangoo's asafoetida was out of stock. How it was collected: the page-reading tool returned summaries of the JSON, which I transcribed, so this check targets transcription error.
- **Pack-size parser:** one bug was found and fixed ("w320 - 250 g" was read as a range), and it is covered by a unit test.
- `python3 scripts/validate.py` covers referential integrity, grade recomputation, URL evidence for every Confirmed grade, alt-group and signature sanity, scope caps and scenario references.

## 6. Limitations

- **Single-source risk:** only 32 ingredients are Confirmed by at least 2 retailers. The rest rest on one catalogue, mostly Waangoo or Kiasumart.
- **Online bias:** physical Little India shops (Mustafa, Tekka market) have no machine-readable catalogues. Items like fresh gongura may exist there seasonally, and this data cannot see that. The "absent after 3 sources" rule makes such items *Difficult*, not *Unavailable*.
- FairPrice evidence proves a product page exists, not that it's in stock.
- SingStat reference prices are from Dec 2024, the latest month in the data.gov.sg resource at retrieval.
- **Human review (2026-09-29):** all 46 substitutions carry a human label (`human_label`), and the `is_signature` markings for all 15 dishes were reviewed and confirmed by the project author. The `jain_ok` edge cases remain my own proposals. Eval ground truth is generated from the human labels only.
- Gram conversions for cups and pieces are estimates. Each conversion is documented in `conversion_note`.
