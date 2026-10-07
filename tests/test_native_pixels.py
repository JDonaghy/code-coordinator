"""Tests for :mod:`coord.native_pixels` — the shared capture-decode + region
-judgment logic every Tier-2 native driver's ``expect_region_not_uniform``/
``expect_no_tofu`` steps call through (#3650).

Each decoder is exercised against a hand-built byte buffer matching exactly
the one concrete shape its own driver's real capture call produces (see the
module's own docstring) — a minimal from-spec encoder, not a round-trip
through a third-party imaging library (deliberately not a dependency here).
"""

from __future__ import annotations

import struct
import zlib

import pytest

from coord.native_pixels import (
    NativePixelError,
    PixelImage,
    decode_bmp,
    decode_png,
    decode_xwd,
    looks_like_tofu,
    region_not_uniform,
)


# ── encoders used only to build test fixtures ───────────────────────────────

def _encode_png_rgb(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """A minimal from-spec PNG encoder: 8-bit RGB (color type 2),
    non-interlaced, every scanline filter type 0 (None)."""
    height = len(rows)
    width = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0
        for (r, g, b) in row:
            raw += bytes((r, g, b))
    compressed = zlib.compress(bytes(raw))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def _encode_bmp_rgb(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """A minimal 24-bit, bottom-up, uncompressed BMP — exactly the shape
    :func:`coord.win_native_driver._bitmap_to_bmp_bytes` writes."""
    height = len(rows)
    width = len(rows[0])
    row_bytes = ((width * 3 + 3) // 4) * 4
    body = bytearray()
    for row in reversed(rows):  # bottom-up on disk
        line = bytearray(row_bytes)
        for i, (r, g, b) in enumerate(row):
            line[i * 3:i * 3 + 3] = bytes((b, g, r))
        body += line
    info_header = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(body), 0, 0, 0, 0,
    )
    pixel_offset = 14 + len(info_header)
    file_header = struct.pack("<2sIHHI", b"BM", pixel_offset + len(body), 0, 0, pixel_offset)
    return file_header + info_header + bytes(body)


def _encode_xwd_rgb(
    rows: list[list[tuple[int, int, int]]],
    *,
    ncolors: int = 0,
    byte_order: int = 1,
    bits_per_pixel: int = 32,
) -> bytes:
    """A ZPixmap, 24 or 32bpp, true-color XWD dump — matching the *real*
    on-disk shape ``xwd`` itself writes: a ``colormap`` of ``ncolors``
    12-byte ``XWDColor`` entries sits between the header and the pixel
    data (``XWDFile.h:96-99``), which the two hand-rolled test-only
    encoders in this repo originally omitted (hardcoding ``ncolors=0``) —
    exactly the gap that let a wrong :func:`coord.native_pixels.decode_xwd`
    offset go uncaught. Defaults match a headless Xvfb's actual TrueColor
    dump has no colormap entries, but every caller below exercises
    ``ncolors > 0`` too.

    ``byte_order``/``bits_per_pixel`` are parameterized so tests can cover
    the ``"<I"`` (LSBFirst, the branch that actually runs on real x86
    hardware) and 24bpp (``padded = raw_word + b"\\x00"``) decode paths,
    not just the MSBFirst/32bpp shape the original fixture hardcoded.
    """
    height = len(rows)
    width = len(rows[0])
    window_name = b"test\x00"
    header_size = 25 * 4 + len(window_name)
    bytes_pp = bits_per_pixel // 8
    bytes_per_line = width * bytes_pp
    header = struct.pack(
        ">25I",
        header_size,  # header_size
        7,            # file_version
        2,            # pixmap_format (ZPixmap)
        24,           # pixmap_depth
        width,        # pixmap_width
        height,       # pixmap_height
        0,            # xoffset
        byte_order,
        32,           # bitmap_unit
        1,            # bitmap_bit_order
        32,           # bitmap_pad
        bits_per_pixel,
        bytes_per_line,
        4,            # visual_class (TrueColor)
        0xFF0000,     # red_mask
        0x00FF00,     # green_mask
        0x0000FF,     # blue_mask
        8,            # bits_per_rgb
        ncolors,      # colormap_entries
        ncolors,      # ncolors
        width,        # window_width
        height,       # window_height
        0, 0, 0,      # window_x, window_y, window_bdrwidth
    )
    colormap = b"\x00" * (12 * ncolors)  # sz_XWDColor == 12, content irrelevant here
    fmt = ">I" if byte_order == 1 else "<I"
    body = bytearray()
    for row in rows:
        for (r, g, b) in row:
            value = (r << 16) | (g << 8) | b
            packed = struct.pack(fmt, value)
            body += packed if bytes_pp == 4 else (packed[1:] if byte_order == 1 else packed[:3])
    return header + window_name + colormap + bytes(body)


_SAMPLE_ROWS = [
    [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 10, 10)],
    [(255, 255, 255), (128, 128, 128), (64, 64, 64), (0, 0, 0)],
    [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)],
]

_UNIFORM_ROWS = [[(50, 50, 50)] * 4 for _ in range(4)]


# ── decode_png ───────────────────────────────────────────────────────────────

def test_decode_png_round_trip():
    data = _encode_png_rgb(_SAMPLE_ROWS)
    image = decode_png(data)
    assert image.width == 4 and image.height == 3
    for y, row in enumerate(_SAMPLE_ROWS):
        for x, pixel in enumerate(row):
            assert image.get(x, y) == pixel


def test_decode_png_rejects_bad_signature():
    with pytest.raises(NativePixelError, match="signature"):
        decode_png(b"not a png at all")


def test_decode_png_rejects_16bit_depth():
    data = bytearray(_encode_png_rgb(_SAMPLE_ROWS))
    # Patch the IHDR bit-depth byte (offset 8 (sig) + 8 (chunk hdr) + 8
    # (width/height) == byte 24) to 16.
    ihdr_bit_depth_offset = 8 + 8 + 8
    data[ihdr_bit_depth_offset] = 16
    with pytest.raises(NativePixelError, match="bit depth"):
        decode_png(bytes(data))


# ── decode_bmp ───────────────────────────────────────────────────────────────

def test_decode_bmp_round_trip():
    data = _encode_bmp_rgb(_SAMPLE_ROWS)
    image = decode_bmp(data)
    assert image.width == 4 and image.height == 3
    for y, row in enumerate(_SAMPLE_ROWS):
        for x, pixel in enumerate(row):
            assert image.get(x, y) == pixel


def test_decode_bmp_rejects_bad_magic():
    with pytest.raises(NativePixelError, match="magic"):
        decode_bmp(b"XX" + b"\x00" * 100)


def test_decode_bmp_rejects_non_24bit():
    data = bytearray(_encode_bmp_rgb(_SAMPLE_ROWS))
    struct.pack_into("<H", data, 28, 32)  # claim 32bpp
    with pytest.raises(NativePixelError, match="bit depth"):
        decode_bmp(bytes(data))


# ── decode_xwd ───────────────────────────────────────────────────────────────

def test_decode_xwd_round_trip():
    data = _encode_xwd_rgb(_SAMPLE_ROWS)
    image = decode_xwd(data)
    assert image.width == 4 and image.height == 3
    for y, row in enumerate(_SAMPLE_ROWS):
        for x, pixel in enumerate(row):
            assert image.get(x, y) == pixel


def test_decode_xwd_rejects_truncated_header():
    with pytest.raises(NativePixelError, match="truncated"):
        decode_xwd(b"\x00" * 10)


def test_decode_xwd_rejects_non_zpixmap():
    data = bytearray(_encode_xwd_rgb(_SAMPLE_ROWS))
    struct.pack_into(">I", data, 2 * 4, 1)  # pixmap_format <- XYPixmap == 1
    with pytest.raises(NativePixelError, match="ZPixmap"):
        decode_xwd(bytes(data))


def test_decode_xwd_skips_colormap_between_header_and_pixel_data():
    """Regression for the real bug this issue's review caught: a real
    ``xwd`` dump (e.g. ``ncolors=256`` on a typical TrueColor X11 display)
    carries a colormap of ``ncolors`` 12-byte ``XWDColor`` entries between
    the header and the pixel data (``XWDFile.h:96-99``) — decoding must
    skip past it, not start reading pixels right after the header."""
    data = _encode_xwd_rgb(_SAMPLE_ROWS, ncolors=256)
    image = decode_xwd(data)
    assert image.width == 4 and image.height == 3
    for y, row in enumerate(_SAMPLE_ROWS):
        for x, pixel in enumerate(row):
            assert image.get(x, y) == pixel


def test_decode_xwd_round_trip_lsbfirst_byte_order():
    """Real x86 hosts write ``byte_order=0`` (LSBFirst) — the ``"<I"``
    decode branch — not the MSBFirst shape the original fixture hardcoded."""
    data = _encode_xwd_rgb(_SAMPLE_ROWS, ncolors=4, byte_order=0)
    image = decode_xwd(data)
    assert image.width == 4 and image.height == 3
    for y, row in enumerate(_SAMPLE_ROWS):
        for x, pixel in enumerate(row):
            assert image.get(x, y) == pixel


def test_decode_xwd_round_trip_24bpp():
    """The 24bpp decode path (``padded = raw_word + b"\\x00"``/
    ``b"\\x00" + raw_word``) is distinct from the 32bpp one and needs its
    own coverage, for both byte orders."""
    for byte_order in (0, 1):
        data = _encode_xwd_rgb(
            _SAMPLE_ROWS, ncolors=16, byte_order=byte_order, bits_per_pixel=24,
        )
        image = decode_xwd(data)
        assert image.width == 4 and image.height == 3
        for y, row in enumerate(_SAMPLE_ROWS):
            for x, pixel in enumerate(row):
                assert image.get(x, y) == pixel


# ── region_not_uniform ──────────────────────────────────────────────────────

def test_region_not_uniform_true_for_varied_region():
    image = decode_png(_encode_png_rgb(_SAMPLE_ROWS))
    is_not_uniform, _msg = region_not_uniform(image, 0, 0, 4, 3)
    assert is_not_uniform is True


def test_region_not_uniform_false_for_flat_region():
    image = decode_png(_encode_png_rgb(_UNIFORM_ROWS))
    is_not_uniform, msg = region_not_uniform(image, 0, 0, 4, 4)
    assert is_not_uniform is False
    assert "uniform" in msg


def test_region_not_uniform_respects_tolerance():
    rows = [[(100, 100, 100), (105, 105, 105)], [(100, 100, 100), (102, 102, 102)]]
    image = decode_png(_encode_png_rgb(rows))
    is_not_uniform, _ = region_not_uniform(image, 0, 0, 2, 2, tolerance=10)
    assert is_not_uniform is False
    is_not_uniform, _ = region_not_uniform(image, 0, 0, 2, 2, tolerance=2)
    assert is_not_uniform is True


def test_region_not_uniform_rejects_out_of_bounds_region():
    image = decode_png(_encode_png_rgb(_UNIFORM_ROWS))
    with pytest.raises(NativePixelError, match="does not fit"):
        region_not_uniform(image, 0, 0, 100, 100)


# ── looks_like_tofu ──────────────────────────────────────────────────────────

def _tofu_box_rows(size: int = 8, border=(0, 0, 0), interior=(255, 255, 255)) -> list:
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            if x in (0, size - 1) or y in (0, size - 1):
                row.append(border)
            else:
                row.append(interior)
        rows.append(row)
    return rows


def test_looks_like_tofu_true_for_classic_box():
    image = decode_png(_encode_png_rgb(_tofu_box_rows()))
    is_tofu, msg = looks_like_tofu(image, 0, 0, 8, 8)
    assert is_tofu is True
    assert "tofu" in msg


def test_looks_like_tofu_false_for_uniform_region():
    image = decode_png(_encode_png_rgb(_UNIFORM_ROWS))
    is_tofu, _msg = looks_like_tofu(image, 0, 0, 4, 4)
    assert is_tofu is False


def test_looks_like_tofu_false_for_textured_glyph():
    # A region with internal variation (not a flat interior) should not
    # read as the classic placeholder box.
    image = decode_png(_encode_png_rgb(_SAMPLE_ROWS * 3))
    is_tofu, _msg = looks_like_tofu(image, 0, 0, 4, 3)
    assert is_tofu is False


def test_looks_like_tofu_rejects_too_small_region():
    image = decode_png(_encode_png_rgb(_tofu_box_rows()))
    is_tofu, msg = looks_like_tofu(image, 0, 0, 2, 2)
    assert is_tofu is False
    assert "too small" in msg
