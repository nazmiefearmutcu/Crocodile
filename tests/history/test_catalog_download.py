import asyncio
import hashlib
import json
from datetime import date

import pytest

from crocodile.crypto.history import (
    ArchiveObject,
    ArchiveRequest,
    DownloadError,
    ExpiredArchiveURL,
    download_archive,
    list_okx_archives,
    parse_bybit_listing,
    parse_zenodo_record,
)


class Response:
    def __init__(self, status=200, payload=None, body=b"", headers=None, cancel=False):
        self.status = status
        self.payload = payload
        self.body = body
        self.headers = headers or {}
        self.content = self
        self.cancel = cancel

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.payload

    async def iter_chunked(self, size):
        midpoint = len(self.body) // 2 or len(self.body)
        yield self.body[:midpoint]
        if self.cancel:
            raise asyncio.CancelledError
        yield self.body[midpoint:]


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def obj(filename="BTC-USDT-L2orderbook-400lv-2026-10-07.tar.gz", **kwargs):
    return ArchiveObject(
        venue="okx",
        product="SPOT",
        symbol="BTC-USDT",
        module=4,
        source_date="2026-10-07",
        filename=filename,
        url="https://static.okx.com/cdn/" + filename,
        format="tar-ndjson",
        depth=400,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_listing_unavailable_and_exact_api_contract():
    request = ArchiveRequest("BTC-USDT", "SPOT", 4, date(2026, 10, 7), date(2026, 10, 8))
    session = Session(
        Response(
            payload={
                "code": "0",
                "data": [
                    {
                        "details": [
                            {
                                "instId": "BTC-USDT",
                                "instType": "SPOT",
                                "groupDetails": [
                                    {
                                        "dateTs": "1791331200000",
                                        "filename": obj().filename,
                                        "sizeMB": "100.11",
                                        "url": obj().url,
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        )
    )
    listing = await list_okx_archives(request, session=session)
    assert listing.objects[0].symbol == "BTC-USDT"
    # A metadata response has not inspected this object's payload or its complete tail.
    assert listing.objects[0].schema_status == "object_not_audited"
    assert listing.missing_dates == ("2026-10-08",)
    params = session.calls[0][1]["params"]
    assert params["module"] == "4"
    assert params["instIdList"] == "BTC-USDT"
    assert params["begin"] == "1791331200000"
    assert params["end"] == "1791417600000"


@pytest.mark.asyncio
async def test_listing_splits_large_range_without_hiding_missing_days():
    session = Session(
        *[Response(payload={"code": "0", "data": [{"details": []}]}) for _ in range(2)]
    )
    result = await list_okx_archives(
        ArchiveRequest("BTC-USDT-SWAP", "SWAP", 5, date(2026, 9, 1), date(2026, 9, 11)),
        session=session,
    )
    assert len(session.calls) == 2
    assert len(result.missing_dates) == 11
    assert session.calls[0][1]["params"]["instFamilyList"] == "BTC-USDT"


@pytest.mark.asyncio
async def test_download_hash_atomic_manifest_and_actual_capture_time(tmp_path):
    body = b"a real bounded archive"
    session = Session(
        Response(body=body, headers={"Content-Length": str(len(body)), "ETag": '"v1"'})
    )
    manifest = await download_archive(
        obj(),
        tmp_path,
        max_bytes=100,
        expected_sha256=hashlib.sha256(body).hexdigest(),
        session=session,
        imported_at_ns=1234,
    )
    assert (tmp_path / obj().filename).read_bytes() == body
    assert manifest.sha256 == hashlib.sha256(body).hexdigest()
    assert manifest.imported_at_ns == 1234
    assert manifest.continuity == "source-sequence-unavailable"
    saved = json.loads((tmp_path / (obj().filename + ".manifest.json")).read_text())
    assert saved["bytes"] == len(body)
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_size_hash_expired_url_and_host_are_loud_failures(tmp_path):
    with pytest.raises(DownloadError, match="cap"):
        await download_archive(
            obj(), tmp_path, max_bytes=3, session=Session(Response(body=b"four"))
        )
    assert not (tmp_path / obj().filename).exists()
    with pytest.raises(DownloadError, match="SHA256"):
        await download_archive(
            obj(),
            tmp_path,
            max_bytes=100,
            expected_sha256="0" * 64,
            session=Session(Response(body=b"data")),
        )
    with pytest.raises(ExpiredArchiveURL):
        await download_archive(
            obj(), tmp_path, max_bytes=100, session=Session(Response(status=403))
        )
    bad = obj()
    from dataclasses import replace

    with pytest.raises(DownloadError, match="host"):
        await download_archive(
            replace(bad, url="https://attacker.invalid/a"),
            tmp_path,
            max_bytes=100,
            session=Session(),
        )
    with pytest.raises(ValueError, match="filename"):
        obj("../escape.tar.gz")


@pytest.mark.asyncio
async def test_cancel_resume_requires_content_range_and_object_identity(tmp_path):
    body = b"abcdefghij"
    with pytest.raises(asyncio.CancelledError):
        await download_archive(
            obj(),
            tmp_path,
            max_bytes=100,
            session=Session(
                Response(body=body, headers={"Content-Length": "10", "ETag": '"same"'}, cancel=True)
            ),
        )
    assert not (tmp_path / obj().filename).exists()
    session = Session(
        Response(
            status=206,
            body=body[5:],
            headers={"Content-Range": "bytes 5-9/10", "Content-Length": "5", "ETag": '"same"'},
        )
    )
    await download_archive(obj(), tmp_path, max_bytes=100, session=session)
    assert session.calls[0][1]["headers"]["Range"] == "bytes=5-"
    assert (tmp_path / obj().filename).read_bytes() == body


@pytest.mark.asyncio
async def test_bad_range_never_appends_and_changed_etag_restarts(tmp_path):
    with pytest.raises(asyncio.CancelledError):
        await download_archive(
            obj(),
            tmp_path,
            max_bytes=100,
            session=Session(
                Response(
                    body=b"abcdefghij",
                    headers={"Content-Length": "10", "ETag": '"old"'},
                    cancel=True,
                )
            ),
        )
    with pytest.raises(DownloadError, match="Content-Range"):
        await download_archive(
            obj(),
            tmp_path,
            max_bytes=100,
            session=Session(
                Response(
                    status=206,
                    body=b"fghij",
                    headers={"Content-Range": "bytes 4-8/10", "ETag": '"old"'},
                )
            ),
        )
    session = Session(
        Response(
            status=206, body=b"new!!", headers={"Content-Range": "bytes 5-9/10", "ETag": '"new"'}
        ),
        Response(body=b"replacement", headers={"Content-Length": "11", "ETag": '"new"'}),
    )
    await download_archive(obj(), tmp_path, max_bytes=100, session=session)
    assert (tmp_path / obj().filename).read_bytes() == b"replacement"
    assert "Range" not in session.calls[1][1]["headers"]


def test_other_sources_are_schema_audits_never_binance_history():
    bybit = parse_bybit_listing(
        '<a href="2026-10-07_BTCUSDT_ob200.data.zip">a</a>', category="linear", symbol="BTCUSDT"
    )
    assert bybit[0].venue == "bybit" and bybit[0].depth == 200
    assert bybit[0].schema_status == "unaudited"
    zenodo = parse_zenodo_record(
        {
            "id": 18184441,
            "metadata": {"license": {"id": "cc-by-4.0"}, "title": "An Open Book"},
            "files": [
                {
                    "key": "book-diffs.tar.gz",
                    "size": 100,
                    "checksum": "md5:" + "1" * 32,
                    "links": {
                        "self": "https://zenodo.org/api/records/18184441/files/book-diffs.tar.gz/content"
                    },
                }
            ],
        }
    )
    assert zenodo[0].venue == "hyperliquid"
    assert zenodo[0].schema_status == "unsupported_until_timestamp_and_opening_state_audit"
    assert zenodo[0].license == "cc-by-4.0"


@pytest.mark.parametrize("filename", ["CON", "nul.gz", "LPT1.tar.gz", "name. ", "name."])
def test_windows_device_or_alias_filename_is_rejected(filename):
    with pytest.raises(ValueError, match="filename"):
        obj(filename)


@pytest.mark.asyncio
async def test_download_resumed_bytes_are_verified_before_appending(tmp_path):
    with pytest.raises(asyncio.CancelledError):
        await download_archive(
            obj(),
            tmp_path,
            max_bytes=100,
            session=Session(
                Response(
                    body=b"abcdefghij",
                    cancel=True,
                    headers={"Content-Length": "10", "ETag": '"same"'},
                )
            ),
        )
    (tmp_path / (obj().filename + ".part")).write_bytes(b"xxxxx")
    session = Session(Response(body=b"replacement", headers={"Content-Length": "11"}))
    await download_archive(obj(), tmp_path, max_bytes=100, session=session)
    assert "Range" not in session.calls[0][1]["headers"]
    assert (tmp_path / obj().filename).read_bytes() == b"replacement"
