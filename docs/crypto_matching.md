# Structured Crypto Matching

Polylens matches crypto markets before falling back to broad text similarity. The structured matcher parses Polymarket and Kalshi market text into:

- asset symbol and asset name, such as BTC/Bitcoin or ETH/Ethereum
- target price or range bounds
- direction: `above`, `below`, `between`, or `touches`
- expiry date from title text or Kalshi close time
- market type: price target, range, all-time high, daily close, weekly close, or monthly close

## Matching Rules

Crypto candidates require the same asset, compatible direction, compatible target price or range, compatible expiry window, and compatible market type when available. Missing or ambiguous target/expiry fields fail closed and do not create matches.

Matching priority in `compare-kalshi` and `scan-arb` is:

1. structured sports matcher
2. structured crypto matcher
3. conservative text fallback

The report JSON shape is unchanged. Crypto matches appear in the existing `cross_platform_arbitrage_candidates` section and flow into `price_aware_arbitrage_candidates` when pricing is requested.

## Limitations

The matcher does not prove that two markets share identical resolution rules. It only identifies likely overlap from public titles, subtitles, rules text, tickers, close times, and wallet-traded Polymarket titles. Review candidate explanations and market rules before using them for trading decisions.
