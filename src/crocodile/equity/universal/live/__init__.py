"""Live market state for listings outside the daily-bar pipeline: books, quotes and prints.

The universal sources of :mod:`crocodile.equity.universal` collect listings and daily bars. This
package answers the other half of what a crypto venue gives for free: what a listing is doing
now. :mod:`.model` is the snapshot and the records it supports, :mod:`.base` the source
contract, :mod:`.sources` one module per publisher, :mod:`.registry` the per-market chain and
its coverage table, and :mod:`.feed` the poller that walks a chain.

The package is ASCII-only, as every new file in ``equity/universal`` must be.
"""
