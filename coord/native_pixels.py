"""Shared pixel-region analysis for every Tier-2 native driver (#3650).

``expect_region_not_uniform``/``expect_no_tofu`` both reduce to the same
question — "decode this driver's own capture format into a pixel grid, then
judge one rectangular region of it" — regardless of which driver's own
window-capture call produced the bytes:
:meth:`coord.mac_native_driver.MacCalls.capture` always hands back a PNG
(``screencapture -x``), :meth:`coord.win_native_driver.WinCalls.capture`
always hands back the BMP :func:`coord.win_native_driver._bitmap_to_bmp_bytes`
builds, and :meth:`coord.gtk_native_driver.GtkCalls.capture` always hands
back an ``xwd`` dump. The *decode* step is necessarily format-specific (three
different functions below, one per format) — but the *judgment* on the
resulting pixel grid is not, so it lives ONCE here
(:func:`region_not_uniform`/:func:`looks_like_tofu`) and every driver calls
through it, rather than four independently-written (and inevitably
drifting) copies of "is this region a single colour" (#2096 "one question,
one answer").

Deliberately no new third-party imaging dependency (no Pillow): each decoder
below handles exactly the one concrete shape its own driver's capture call
actually produces (documented on each), not the general case of its format
— ``decode_png`` only needs 8-bit-depth, non-interlaced PNG (color types
0/2/4/6, no palette), ``decode_bmp`` only needs the specific uncompressed
24-bit bottom-up DIB :mod:`coord.win_native_driver` itself writes, and
``decode_xwd`` only needs a ``ZPixmap`` dump at 24 or 32 bits per pixel (the
overwhelming common case for any modern X11 display, headless Xvfb included).
Anything outside those shapes raises :class:`NativePixelError` naming
exactly what wasn't supported, rather than silently misreading garbage
pixels.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


class NativePixelError(Exception):
    """Raised when a capture can't be decoded (unrecognized/unsupported
    format shape) or when a requested region doesn't fit inside the decoded
    image."""


@dataclass(frozen=True)
class PixelImage:
    """A decoded capture — ``rows[y][x]`` is the ``(r, g, b)`` pixel at
    ``(x, y)``, 0-indexed from the top-left, the same coordinate convention
    every driver's own ``click``/``expect_region_not_uniform``/
    ``expect_no_tofu`` fields already use."""

    width: int
    height: int
    rows: tuple[tuple[tuple[int, int, int], ...], ...]

    def get(self, x: int, y: int) -> tuple[int, int, int]:
        return self.rows[y][x]

    def crop(self, x: int, y: int, width: int, height: int) -> list[tuple[int, int, int]]:
        """Every pixel in the ``(x, y, width, height)`` rectangle, raising
        :class:`NativePixelError` (rather than an ``IndexError`` deep inside
        a caller's loop) when it doesn't fit inside this image at all."""
        if width <= 0 or height <= 0:
            raise NativePixelError(f"region width/height must be positive, got {width}x{height}")
        if x < 0 or y < 0 or x + width > self.width or y + height > self.height:
            raise NativePixelError(
                f"region ({x},{y},{width}x{height}) does not fit inside the "
                f"captured {self.width}x{self.height} image"
            )
        return [
            self.rows[row][col]
            for row in range(y, y + height)
            for col in range(x, x + width)
        ]


# ── PNG (mac-native's screencapture output) ─────────────────────────────────

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: color type -> number of channels. Palette (3) is deliberately absent —
#: see the module docstring's "exactly the one concrete shape" scoping note;
#: `screencapture -x` never emits a palette image.
_PNG_CHANNELS: dict[int, int] = {0: 1, 2: 3, 4: 2, 6: 4}


def decode_png(data: bytes) -> PixelImage:
    """Decode an 8-bit-depth, non-interlaced PNG (color type 0/2/4/6 —
    grayscale, RGB, grayscale+alpha, or RGBA) into a :class:`PixelImage`.
    Raises :class:`NativePixelError` for anything else (wrong signature,
    palette-based, interlaced, >8-bit depth, truncated/corrupt chunks) —
    exactly the shapes macOS's own ``screencapture -x`` never produces, so a
    raise here means the capture itself is unexpectedly not a plain
    screenshot, not that this decoder needs to grow a new case."""
    if not data.startswith(_PNG_SIGNATURE):
        raise NativePixelError("not a PNG — missing the 8-byte PNG signature")

    offset = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    while offset < len(data):
        if offset + 8 > len(data):
            raise NativePixelError("truncated PNG — incomplete chunk header")
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4:offset + 8].decode("ascii", errors="replace")
        chunk_start = offset + 8
        chunk_end = chunk_start + length
        if chunk_end > len(data):
            raise NativePixelError(f"truncated PNG — {chunk_type} chunk runs past end of file")
        chunk_data = data[chunk_start:chunk_end]
        if chunk_type == "IHDR":
            (width, height, bit_depth, color_type, _compression, _filter_method,
             interlace) = struct.unpack(">IIBBBBB", chunk_data)
        elif chunk_type == "IDAT":
            idat += chunk_data
        elif chunk_type == "IEND":
            break
        offset = chunk_end + 4  # skip the trailing 4-byte CRC

    if width is None:
        raise NativePixelError("malformed PNG — no IHDR chunk found")
    if bit_depth != 8:
        raise NativePixelError(f"unsupported PNG bit depth {bit_depth} — only 8-bit is handled")
    if color_type not in _PNG_CHANNELS:
        raise NativePixelError(f"unsupported PNG color type {color_type} (e.g. palette-based)")
    if interlace:
        raise NativePixelError("unsupported PNG — interlaced images are not handled")

    channels = _PNG_CHANNELS[color_type]
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise NativePixelError(f"PNG IDAT failed to decompress: {e}") from e

    stride = width * channels
    expected_len = (stride + 1) * height
    if len(raw) < expected_len:
        raise NativePixelError(
            f"decompressed PNG data too short: got {len(raw)} bytes, expected {expected_len}"
        )

    rows: list[tuple[tuple[int, int, int], ...]] = []
    prev = bytes(stride)
    pos = 0
    for _y in range(height):
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        _unfilter_png_scanline(filter_type, line, prev, channels)
        rows.append(_png_line_to_rgb(bytes(line), width, channels))
        prev = bytes(line)
    return PixelImage(width=width, height=height, rows=tuple(rows))


def _unfilter_png_scanline(filter_type: int, line: bytearray, prev: bytes, channels: int) -> None:
    """Reverse one PNG scanline filter (Sub/Up/Average/Paeth) IN PLACE, per
    the PNG spec's own reference algorithm — ``line`` holds the still-
    filtered bytes on entry and the reconstructed raw bytes on return."""
    if filter_type == 0:  # None
        return
    length = len(line)
    if filter_type == 1:  # Sub
        for i in range(length):
            a = line[i - channels] if i >= channels else 0
            line[i] = (line[i] + a) & 0xFF
    elif filter_type == 2:  # Up
        for i in range(length):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif filter_type == 3:  # Average
        for i in range(length):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            line[i] = (line[i] + (a + b) // 2) & 0xFF
    elif filter_type == 4:  # Paeth
        for i in range(length):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            line[i] = (line[i] + _paeth_predictor(a, b, c)) & 0xFF
    else:
        raise NativePixelError(f"unsupported PNG filter type {filter_type}")


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_line_to_rgb(line: bytes, width: int, channels: int) -> tuple[tuple[int, int, int], ...]:
    pixels = []
    for x in range(width):
        base = x * channels
        if channels == 1:  # grayscale
            g = line[base]
            pixels.append((g, g, g))
        elif channels == 2:  # grayscale + alpha
            g = line[base]
            pixels.append((g, g, g))
        elif channels == 3:  # RGB
            pixels.append((line[base], line[base + 1], line[base + 2]))
        else:  # RGBA
            pixels.append((line[base], line[base + 1], line[base + 2]))
    return tuple(pixels)


# ── BMP (win-native's PrintWindow output) ───────────────────────────────────

def decode_bmp(data: bytes) -> PixelImage:
    """Decode the specific uncompressed, 24-bit-per-pixel, bottom-up DIB
    :func:`coord.win_native_driver._bitmap_to_bmp_bytes` writes. Raises
    :class:`NativePixelError` for anything else (wrong magic, non-24bpp,
    compressed, top-down) — :mod:`coord.win_native_driver` never produces
    those shapes, so a raise here means the bytes didn't actually come from
    that function."""
    if len(data) < 14 + 40 or data[:2] != b"BM":
        raise NativePixelError("not a BMP — missing the 'BM' magic header")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    header_size = struct.unpack_from("<I", data, 14)[0]
    if header_size < 40:
        raise NativePixelError(f"unsupported BMP info-header size {header_size}")
    width, height = struct.unpack_from("<ii", data, 18)
    bit_count = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if bit_count != 24:
        raise NativePixelError(f"unsupported BMP bit depth {bit_count} — only 24-bit is handled")
    if compression != 0:
        raise NativePixelError(f"unsupported BMP compression {compression} — only BI_RGB is handled")
    bottom_up = height > 0
    height = abs(height)
    row_bytes = ((width * 3 + 3) // 4) * 4
    needed = pixel_offset + row_bytes * height
    if len(data) < needed:
        raise NativePixelError(
            f"truncated BMP pixel data: have {len(data)} bytes, need {needed}"
        )

    file_rows: list[tuple[tuple[int, int, int], ...]] = []
    for row_index in range(height):
        row_start = pixel_offset + row_index * row_bytes
        row = data[row_start:row_start + width * 3]
        pixels = tuple(
            (row[i + 2], row[i + 1], row[i])  # BGR -> RGB
            for i in range(0, width * 3, 3)
        )
        file_rows.append(pixels)
    # BMP stores bottom-up by default (positive height) — reverse so
    # rows[0] is the TOP row, matching :class:`PixelImage`'s own convention.
    rows = tuple(reversed(file_rows)) if bottom_up else tuple(file_rows)
    return PixelImage(width=width, height=height, rows=rows)


# ── XWD (gtk-native's xwd output) ────────────────────────────────────────────

#: XWD file-header field count/layout (X11's ``XWDFile.h``) — 25 CARD32
#: fields, big-endian on every host (xwd always writes it in network byte
#: order, regardless of the local machine's own endianness).
_XWD_HEADER_FIELDS = 25
_XWD_ZPIXMAP = 2


def decode_xwd(data: bytes) -> PixelImage:
    """Decode an ``xwd -id <window>`` dump whose pixmap format is
    ``ZPixmap`` at 24 or 32 bits per pixel, true-color (the shape any modern
    X11 display — including a headless Xvfb session — produces; nothing in
    this fleet runs an 8-bit palette/StaticGray display). Raises
    :class:`NativePixelError` for anything else (wrong/truncated header,
    ``XYPixmap``, any other bit depth)."""
    if len(data) < _XWD_HEADER_FIELDS * 4:
        raise NativePixelError("not an XWD dump — truncated file header")
    header = struct.unpack_from(f">{_XWD_HEADER_FIELDS}I", data, 0)
    # Field order per XWDFileHeader: header_size, file_version, pixmap_format,
    # pixmap_depth, pixmap_width, pixmap_height, xoffset, byte_order,
    # bitmap_unit, bitmap_bit_order, bitmap_pad, bits_per_pixel,
    # bytes_per_line, visual_class, red_mask, green_mask, blue_mask,
    # bits_per_rgb, colormap_entries, ncolors, window_width, window_height,
    # window_x, window_y, window_bdrwidth.
    (
        header_size, _file_version, pixmap_format, _pixmap_depth, pixmap_width,
        pixmap_height, _xoffset, byte_order, _bitmap_unit, _bitmap_bit_order,
        _bitmap_pad, bits_per_pixel, bytes_per_line, _visual_class,
        red_mask, green_mask, blue_mask, _bits_per_rgb, _colormap_entries,
        _ncolors, _window_width, _window_height, _window_x, _window_y,
        _window_bdrwidth,
    ) = header

    if pixmap_format != _XWD_ZPIXMAP:
        raise NativePixelError(
            f"unsupported XWD pixmap format {pixmap_format} — only ZPixmap is handled"
        )
    if bits_per_pixel not in (24, 32):
        raise NativePixelError(
            f"unsupported XWD bits-per-pixel {bits_per_pixel} — only 24/32bpp true-color is handled"
        )
    if header_size < _XWD_HEADER_FIELDS * 4:
        raise NativePixelError(f"implausible XWD header_size {header_size}")

    pixel_offset = header_size  # the window-name string (NUL-terminated) fills the rest of header_size
    bytes_pp = bits_per_pixel // 8
    needed = pixel_offset + bytes_per_line * pixmap_height
    if len(data) < needed:
        raise NativePixelError(
            f"truncated XWD pixel data: have {len(data)} bytes, need {needed}"
        )

    fmt = ">I" if byte_order == 1 else "<I"  # 1 == MSBFirst
    rows: list[tuple[tuple[int, int, int], ...]] = []
    for y in range(pixmap_height):
        row_start = pixel_offset + y * bytes_per_line
        pixels = []
        for x in range(pixmap_width):
            pixel_start = row_start + x * bytes_pp
            raw_word = data[pixel_start:pixel_start + bytes_pp]
            if bytes_pp == 4:
                value = struct.unpack(fmt, raw_word)[0]
            else:
                padded = raw_word + b"\x00" if fmt == "<I" else b"\x00" + raw_word
                value = struct.unpack(fmt, padded)[0]
            pixels.append(_xwd_pixel_to_rgb(value, red_mask, green_mask, blue_mask))
        rows.append(tuple(pixels))
    return PixelImage(width=pixmap_width, height=pixmap_height, rows=tuple(rows))


def _mask_to_shift(mask: int) -> int:
    if mask == 0:
        return 0
    shift = 0
    while not (mask >> shift) & 1:
        shift += 1
    return shift


def _xwd_pixel_to_rgb(value: int, red_mask: int, green_mask: int, blue_mask: int) -> tuple[int, int, int]:
    def channel(mask: int) -> int:
        shift = _mask_to_shift(mask)
        bits = mask >> shift
        raw = (value & mask) >> shift
        if bits == 0:
            return 0
        # Scale up to 8 bits regardless of the mask's own bit width (true-
        # color masks are 8 bits wide on every display this driver targets,
        # so this is normally a no-op, but stays correct if not).
        max_val = bits
        return min(255, (raw * 255) // max_val) if max_val else 0

    return channel(red_mask), channel(green_mask), channel(blue_mask)


# ── the shared judgment: region_not_uniform / looks_like_tofu ──────────────

def region_not_uniform(
    image: PixelImage, x: int, y: int, width: int, height: int, *, tolerance: int = 24,
) -> tuple[bool, str]:
    """``(True, msg)`` when the ``(x, y, width, height)`` region of *image*
    is NOT a single colour within ± *tolerance* per channel — i.e. the
    region has real visual content. ``(False, msg)`` when every pixel in it
    is within *tolerance* of the region's own first pixel — vimcode#1676's
    "uniform black bar" minimap and #1828's blank panel are exactly this:
    a region that should show a rendered thumbnail/UI but is actually one
    flat colour.

    Raises :class:`NativePixelError` (via :meth:`PixelImage.crop`) when the
    region doesn't fit inside the captured image at all — a malformed-spec
    condition (coordinates from the wrong window size), not a "fail"
    verdict to report as if the pixels were examined.
    """
    pixels = image.crop(x, y, width, height)
    base = pixels[0]
    max_delta = 0
    for pixel in pixels:
        delta = max(abs(pixel[i] - base[i]) for i in range(3))
        max_delta = max(max_delta, delta)
        if max_delta > tolerance:
            break
    if max_delta <= tolerance:
        return False, (
            f"region ({x},{y},{width}x{height}) is uniform — every pixel is "
            f"within {tolerance} of {base!r} (max observed delta {max_delta})"
        )
    return True, f"region ({x},{y},{width}x{height}) varies (max channel delta {max_delta})"


#: #3650: a coarse heuristic, not a font-rendering oracle. A "tofu" glyph
#: (the missing-glyph placeholder box every font-rendering stack falls back
#: to) renders as a thin, high-contrast rectangular OUTLINE enclosing a
#: near-uniform interior — unlike almost every real glyph, which has
#: internal strokes/curves that break up the interior too. This heuristic
#: trades recall for precision: a real glyph that happens to be a simple
#: closed shape at small size (a very low-resolution "o", "0", or a solid
#: block-drawing character) can false-POSITIVE as tofu, and nothing here
#: attempts to rule that out — callers asserting ``expect_no_tofu`` against
#: a specific, known-complex glyph (the common real case: a CJK character,
#: an emoji, a ligature a font doesn't carry) will not see those false
#: positives in practice, but a spec author asserting it broadly against
#: plain ASCII text should expect an occasional false positive on
#: box-drawing/line characters. No attempt is made to estimate a numeric
#: false-negative rate (a real tofu box rendered with anti-aliasing or a
#: non-default accent colour could average below the border-contrast
#: threshold below and be missed) — treat a "no tofu" verdict as "nothing
#: that LOOKS like the classic placeholder box was seen", not a proof.
_TOFU_BORDER_CONTRAST = 40
_TOFU_INTERIOR_TOLERANCE = 24


def looks_like_tofu(image: PixelImage, x: int, y: int, width: int, height: int) -> tuple[bool, str]:
    """``(True, msg)`` when the ``(x, y, width, height)`` region of *image*
    looks like a missing-glyph "tofu" placeholder box (see the coarse
    -heuristic note above) — a thin rectangular border whose colour
    contrasts with its own near-uniform interior. ``(False, msg)`` otherwise
    (including a region that is itself fully uniform — a tofu box is a
    BORDER around something, not a flat fill; a flat fill is
    ``expect_region_not_uniform``'s own concern, not this one's).

    Raises :class:`NativePixelError` when the region doesn't fit inside the
    captured image."""
    if width < 3 or height < 3:
        return False, f"region ({x},{y},{width}x{height}) is too small to judge for tofu"
    pixels = image.crop(x, y, width, height)

    def at(px: int, py: int) -> tuple[int, int, int]:
        return pixels[py * width + px]

    border_pixels = (
        [at(px, 0) for px in range(width)]
        + [at(px, height - 1) for px in range(width)]
        + [at(0, py) for py in range(height)]
        + [at(width - 1, py) for py in range(height)]
    )
    interior_pixels = [
        at(px, py) for py in range(1, height - 1) for px in range(1, width - 1)
    ]
    if not interior_pixels:
        return False, f"region ({x},{y},{width}x{height}) has no interior to compare against a border"

    border_avg = _average_color(border_pixels)
    interior_avg = _average_color(interior_pixels)
    interior_max_delta = max(
        max(abs(p[i] - interior_avg[i]) for i in range(3)) for p in interior_pixels
    )
    border_contrast = max(abs(border_avg[i] - interior_avg[i]) for i in range(3))

    if interior_max_delta <= _TOFU_INTERIOR_TOLERANCE and border_contrast >= _TOFU_BORDER_CONTRAST:
        return True, (
            f"region ({x},{y},{width}x{height}) looks like a tofu/placeholder "
            f"box — near-uniform interior (max delta {interior_max_delta}) "
            f"with a high-contrast border (delta {border_contrast} from "
            f"interior average)"
        )
    return False, (
        f"region ({x},{y},{width}x{height}) does not look like a tofu box — "
        f"interior max delta {interior_max_delta}, border contrast {border_contrast}"
    )


def _average_color(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    n = len(pixels)
    return (
        sum(p[0] for p in pixels) // n,
        sum(p[1] for p in pixels) // n,
        sum(p[2] for p in pixels) // n,
    )
