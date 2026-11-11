"""Official listing directories: the keyless files an exchange or national registry publishes.

A listing directory is a file the market itself publishes, listing the securities that trade on
it. It is a statement of existence on the directory's as-of date, and that is all it is. It does
not publish a currency (the quote sources do), it does not carry a price, and it never asserts a
delisting: a listing absent from a directory is ``unseen`` in the listing master, never
``delisted``. Each directory yields one snapshot per run for the listing master and one
``instrument`` record per listing, so a listing that disappears is still on the day it was last
published.

The four shipped directories are Nasdaq Trader (US), NSE India, ASX and TWSE. ``base`` holds the
rows, the parse result, the snapshot builder (the only place an instrument is built for a
directory) and the provider base class; each market has its own module.
"""
