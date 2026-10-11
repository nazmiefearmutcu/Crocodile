# Independent SBE wire fixtures

Pinned official XML: [stream_1_0.xml at bed5d358dbe98bfa4722b6c00c27be1e8f0ba40b](https://github.com/binance/binance-spot-api-docs/blob/bed5d358dbe98bfa4722b6c00c27be1e8f0ba40b/sbe/schemas/stream_1_0.xml).
`generate.py` imports only stdlib and never invokes the decoder. All integers are little endian.
No credentials or live data are present. These are wire conformance fixtures, not proof of access.

Header absolute offsets 0/2/4/6 are uint16 blockLength/templateId/schemaId/version.
All root offsets below are relative to byte 8. eventTime = 1700000000000000 us,
trade transactTime = eventTime + 1000 us. Symbols use uint8 byte length then UTF-8.

| Template | Root length | Root fields and offsets |
|---|---:|---|
| 10000 trades | 18 | eventTime i64@0, transactTime i64@8, priceExponent i8@16, qtyExponent i8@17 |
| 10001 BBO | 50 | eventTime i64@0, bookUpdateId i64@8, exponents i8@16/17, bidPrice/bidQty/askPrice/askQty i64@18/26/34/42 |
| 10002 snapshot | 18 | eventTime i64@0, bookUpdateId i64@8, exponents i8@16/17 |
| 10003 diff | 26 | eventTime i64@0, first/final IDs i64@8/16, exponents i8@24/25 |

Trades group header `<HI>` begins at absolute26: entry block length25, count1.
Entry at32: ID i64@0=1, price i64@8=3000012, qty i64@16=1250, buyerMaker uint8@24=1.
isBestMatch is constant True and occupies **zero bytes**. Symbol length at57 is7.
Both depth groups use `<HH>` (entry block length16, uint16 count); entries price/qty i64@0/8.
Snapshot contains one bid and an empty ask group. Diff U=99/u=101 contains a bid removal
(qty=0) and an ask replacement. Exponents -2/-3 mean 30000.12 / 1.250; integers are retained.
