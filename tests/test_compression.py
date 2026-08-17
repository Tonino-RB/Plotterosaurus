"""How hard the server is allowed to compress.

Starlette's GZipMiddleware defaults to compresslevel=9. That default shipped
here once and was a straight regression: on this hardware it made the largest
drawings *slower to arrive than sending them uncompressed*.

Measured on the plotter host against a real 7.04MB drawing from the uploads
folder, CPU time (time.process_time, best of three — the board is rarely idle
and wall clock under load is not a measurement), plus transfer at 40Mb/s:

    level    cpu         size    ratio    total
    none    0.00    7,042,190   100.0%    1.41s
       1    0.28    3,045,550    43.2%    0.89s   <- shipping
       2    0.36    2,976,362    42.3%    0.95s
       3    0.77    2,881,802    40.9%    1.35s
       4    0.56    2,829,008    40.2%    1.13s
       5    1.32    2,736,516    38.9%    1.86s   <- crossover
       6    2.24    2,707,796    38.5%    2.78s
       9    3.40    2,696,277    38.3%    3.94s   <- was

Everything from level 5 up costs more CPU than the transfer it saves, so the
whole top half of the scale is strictly worse than sending the bytes raw. SVG
is repetitive enough that level 1 already captures most of the available ratio
— 43.2% against 38.3% at level 9 — so the extra 3.1 seconds of CPU buys 5% of
file size. And that CPU is not free: it comes out of the same four cores the
plot worker, the ink measurement and the optimize queue are competing for, on
a board where sustained load is the thing that overheats it.

(Level 4 beating level 3 is not noise — it reproduces exactly, sizes and all.
zlib changes match strategy at 4, and on this input the new strategy is both
faster and tighter.)

The regression went unnoticed because it was reported as a win from the
compression ratio alone, without ever timing the compression. These tests exist
so the next person changing it has to have that argument on purpose.
"""
import gzip

from app.main import app

# The highest level that still beats sending the bytes uncompressed, from the
# table above; level 5 is where it flips. This number is measured, not chosen —
# reproduce the table on the target hardware before raising it.
MAX_SAFE_LEVEL = 4


def _gzip_config():
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == "GZipMiddleware":
            return getattr(middleware, "kwargs", None) or getattr(middleware, "options", {})
    return None


def test_compression_is_enabled():
    assert _gzip_config() is not None, "GZipMiddleware is not installed"


def test_the_compression_level_is_low_enough_to_be_worth_paying_for():
    """The regression, as a test. An unspecified level means Starlette's 9."""
    config = _gzip_config()
    level = config.get("compresslevel")
    assert level is not None, (
        "compresslevel is unset, so Starlette's default of 9 applies — measured "
        "slower end to end than sending a 7MB drawing uncompressed")
    assert level <= MAX_SAFE_LEVEL, (
        f"compresslevel={level} spends more CPU than the transfer it saves; "
        f"see this module's docstring for the measurements")


def test_small_responses_are_left_alone():
    """Compressing the little JSON the UI polls with costs more than it saves,
    and that polling is frequent."""
    assert _gzip_config().get("minimum_size", 0) >= 512


def test_a_real_response_comes_back_compressed_at_that_level(client):
    """Configuration is one thing; what leaves the socket is another.

    This has to read the *undecoded* bytes. The obvious version of this test —
    take response.content and compare gzip.compress() of it at two levels — is
    a tautology: httpx has already decompressed the body, and level 9 always
    beats level 4 on any input, so the assertion holds no matter what the
    server did. That version passed with compresslevel=9. iter_raw() is what
    makes it an actual measurement of the response.
    """
    with client.stream("GET", "/static/app.js",
                       headers={"Accept-Encoding": "gzip"}) as res:
        assert res.status_code == 200
        assert res.headers.get("content-encoding") == "gzip"
        wire = b"".join(res.iter_raw())

    body = gzip.decompress(wire)
    assert len(body) > 100_000, "app.js should be well over the minimum_size floor"
    assert len(wire) < len(body), "response is labelled gzip but is not smaller"

    # The smallest the body may legitimately be: anything under this was
    # compressed harder than MAX_SAFE_LEVEL. The 2% slack absorbs framing
    # differences between GzipFile and gzip.compress, and is far tighter than
    # the gap to the next level up.
    floor = len(gzip.compress(body, MAX_SAFE_LEVEL))
    assert len(wire) >= floor * 0.98, (
        f"served {len(wire):,} bytes against a level-{MAX_SAFE_LEVEL} floor of "
        f"{floor:,} — the server is compressing harder than the measurements "
        f"in this module's docstring allow")


def test_static_assets_are_cacheable(client):
    """Asset URLs carry a version stamp, so the content behind one never
    changes. Without Cache-Control the browser revalidates every asset on every
    load and waits for four 304s before it can start."""
    cache_control = client.get("/static/app.js").headers.get("cache-control", "")
    assert "max-age=" in cache_control
    assert "immutable" in cache_control
