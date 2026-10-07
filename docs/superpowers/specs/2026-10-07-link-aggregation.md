# Link aggregation with alerts — spec (2026-10-07)

## What the user asked for (their words, lightly structured)

- Main focus right now: **link aggregation with alerts**. "By far the most reliable way to deal with this."
- "I give PriceWatch links, it stores the current price and availability and checks up on it every 60 minutes (configurable in settings as default and per store in the item page)."
- "Stores the information to build a graph of price history (no matter if the price changed)."
- "This should be store agnostic." Each store has different HTML; as we go, dedicated code per provider, but as a standard, generic code for all stores (bar the ones we already know).
- Generic reader: "try a few generic combinations to find out the price and availability. Perhaps if UI is uncertain it could ask the user to confirm the price they're seeing and the availability to help the BE narrow down the correct 'div' to look at — with a warning of course."
- Keep the per-store approach with a generic fallback: keep the dedicated retailer profiles (Worten, FNAC, Darty, Rádio Popular, Amazon ES); add more as needed.
- Out of focus for now: an auto-discovery feature that keeps an eye on the known stores' search results for new entries with similar names.

## Decisions taken to implement this

1. **Store key.** A generic store is identified by its hostname without a leading `www.` (for example `pcdiga.com`). Known stores keep their adapter name (`worten`, …). `Listing.retailer` holds either. Rate limiting, cooldowns and `RetailerState` are keyed the same way, so one store never pauses another.
2. **Reading order for generic pages:** user-confirmed rule → structured data (JSON-LD / microdata, reusing the base adapter parser) → Open Graph / `product:` meta tags → heuristic (elements whose class or id mention price; struck-through, "old price" and recommendation-block prices are demoted). Every snapshot records how it was read: `rule`, `structured`, `meta` or `heuristic`.
3. **Heuristic readings are "price unconfirmed".** They are shown with a warning, recorded in history, but never used for the best price or for monetary alerts until the user confirms a price. Availability and stock-transition events still fire.
4. **Confirming teaches one rule per store.** The user picks one of the prices found on the page or types the one they see; the backend finds the element whose text parses to that amount (shortest text wins) and derives a CSS path. For availability the user picks in stock / out of stock / unknown; the backend derives a text-based selector (an element whose text says so) or, for in stock, a cart-button presence selector; otherwise availability stays heuristic for that store. A rule that stops matching (site redesign) makes the reader fall back to the next tiers and flags the listing again; the rule stays listed in Settings so it can be re-confirmed or forgotten.
5. **Condition on generic pages** defaults to `new` unless the title or page says outlet, refurbished or used — the same rule already applied to Darty and Rádio Popular. The preview shows the condition before saving.
6. **Availability `unknown` never triggers monetary alerts** (existing rule, kept): yesterday's false "insane deal" was an out-of-stock item.
7. **History:** one `price_history` row per successful check, regardless of change. A failed check writes no row (nothing was observed) and keeps `last_error`, so a gap in the chart means "could not check".
8. **Check interval precedence:** listing override (set on the product page) → store override (Settings) → global default (Settings). Range 5–10080 minutes, as today.
9. **Pasted links are first-class.** Every public `https://` host is accepted. Manual candidates are always selectable in the preview and selected by default, even when the page's model conflicts with the product (a warning is shown; discovered candidates with a conflict stay blocked). Non-public hosts (IP literals, `localhost`, single-label names, `.local`/`.lan`/`.internal`/… ) are rejected.
10. **Store search stays available for the known stores** but is collapsed and unchecked by default on the add form: search bursts are what got this IP throttled by Darty and FNAC.
11. **No bypass of anti-bot measures.** Running Playwright when a store answers 429/403 is undecided by the user and not part of this work.
12. **Out of scope:** Shopify `.js` shortcuts, per-host settings beyond the rule list, auto-discovery of similar products.
