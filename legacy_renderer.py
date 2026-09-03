"""Printer-profiled full-raster fallback for explicit legacy mode.

The native mode should be preferred whenever the printer can consume the
source ESC/POS stream.  This renderer exists for the explicit legacy case:
it composes the source tokens in order, uses fixed character cells for text,
and emits one bounded raster image followed by the source cut command.
"""

from dataclasses import dataclass
from typing import Iterable, Optional

from PIL import Image, ImageDraw

from escpos import (
    EscPosToken,
    decode_gs_character_size,
    decode_text,
    parse_stream,
)


class UnsupportedLegacyObject(ValueError):
    """The object cannot be represented safely by a single raster page."""


@dataclass
class LegacyProfile:
    width_dots: int = 384
    columns: int = 32
    font_size: int = 16
    line_spacing: float = 1.0
    margin_dots: int = 0
    image_scale_to_width: bool = True
    image_invert: bool = False
    line_gap: int = 0

    @property
    def cell_width(self) -> int:
        usable = max(1, self.width_dots - (2 * self.margin_dots))
        return max(1, usable // max(1, self.columns))


class LegacyRasterRenderer:
    """Compose a tokenized ESC/POS stream into one printer-sized bitmap."""

    def __init__(self, profile: Optional[LegacyProfile] = None, font=None):
        self.profile = profile or LegacyProfile()
        self.font = font
        self.blocks = []
        self.current_text = ""
        self.align = "left"
        self.bold = False
        self.underline = False
        self.width_mult = 1
        self.height_mult = 1
        self.cut = None
        self.pages = []

    def _snapshot(self):
        return {
            "align": self.align,
            "bold": self.bold,
            "underline": self.underline,
            "width_mult": max(1, min(8, self.width_mult)),
            "height_mult": max(1, min(8, self.height_mult)),
        }

    def _flush(self):
        if self.current_text:
            self.blocks.append({
                "kind": "text",
                "text": self.current_text,
                "format": self._snapshot(),
            })
            self.current_text = ""

    def _newline(self):
        if self.current_text:
            self._flush()
        else:
            # Consecutive LF bytes are meaningful paper feeds.
            self.blocks.append({"kind": "blank"})

    def _before_format_change(self):
        # ESC/POS formatting applies from the current cursor position.  Do not
        # let a command after text retroactively change the preceding text.
        if self.current_text:
            self._flush()

    def consume(self, tokens: Iterable[EscPosToken]):
        for token in tokens:
            raw = token.raw
            if token.kind == "text":
                self.current_text += decode_text(raw)
                continue

            if token.kind == "feed":
                if raw in (b"\n", b"\r", b"\f"):
                    self._newline()
                continue

            if token.kind == "image":
                self._flush()
                self.blocks.append({"kind": "image", "token": token})
                continue

            if token.kind in ("bit_image", "barcode_or_qr", "unknown"):
                # A barcode/QR cannot be faithfully redrawn from its command
                # without its symbology engine.  Falling back is safer than
                # silently printing an incomplete receipt.
                self._flush()
                raise UnsupportedLegacyObject(
                    f"legacy mode cannot rasterize {token.kind} at {token.start}"
                )

            if token.kind == "cut":
                self._flush()
                self.cut = raw
                self.pages.append({"blocks": self.blocks, "cut": raw})
                self.blocks = []
                continue

            if token.kind != "control" or len(raw) < 2:
                continue

            command = raw[1]
            if raw[0] == 0x1D and command == 0x21 and len(raw) >= 3:
                self._before_format_change()
                self.width_mult, self.height_mult = decode_gs_character_size(raw[2])
                continue
            if raw[0] != 0x1B:
                continue

            if command == 0x40:  # ESC @
                self._before_format_change()
                self.align = "left"
                self.bold = False
                self.underline = False
                self.width_mult = 1
                self.height_mult = 1
            elif command == 0x61 and len(raw) >= 3:  # ESC a n
                self._before_format_change()
                self.align = {0: "left", 1: "center", 2: "right"}.get(raw[2], "left")
            elif command == 0x21 and len(raw) >= 3:  # ESC ! n
                self._before_format_change()
                n = raw[2]
                self.bold = bool(n & 0x08)
                self.underline = bool(n & 0x80)
                self.height_mult = 2 if n & 0x10 else 1
                self.width_mult = 2 if n & 0x20 else 1
            elif command == 0x45 and len(raw) >= 3:  # ESC E n
                self._before_format_change()
                self.bold = raw[2] != 0
            elif command == 0x2D and len(raw) >= 3:  # ESC - n
                self._before_format_change()
                self.underline = raw[2] != 0
            elif command == 0x64 and len(raw) >= 3:  # ESC d n
                self._newline()
                for _ in range(raw[2]):
                    self.blocks.append({"kind": "blank"})
            elif command == 0x32:  # ESC 2
                continue
            elif command == 0x33 and len(raw) >= 3:  # ESC 3 n
                continue
            elif command == 0x74:  # ESC t n
                continue
            elif raw[0] == 0x1D and command == 0x42 and len(raw) >= 3:
                # Inverse text is not common in the legacy receipts.  Treating
                # it as an opaque object avoids producing a misleading result.
                self._before_format_change()
                raise UnsupportedLegacyObject("inverse text is not supported")

        self._flush()
        return self

    @staticmethod
    def _clean_text(text: str) -> str:
        return "".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7F)

    @classmethod
    def _effective_format(cls, text: str, fmt: dict) -> dict:
        effective = dict(fmt)
        marker = cls._clean_text(text).lstrip().upper()
        # These receipt identifiers/headings were previously emitted with
        # printer double-size commands.  In legacy mode they use one normal
        # character cell and a light bold stroke so they cannot be clipped.
        if marker.startswith("#SMDC") or marker == "KITCHEN":
            effective.update({
                "bold": True,
                "underline": False,
                "width_mult": 1,
                "height_mult": 1,
            })
        return effective

    def _font_for(self, height_mult=1):
        size = max(8, int(self.profile.font_size * max(1, height_mult)))
        # The caller supplies a font loaded at the base size.  Loading a
        # matching size here keeps glyph metrics stable for each line.
        if self.font is not None and size == self.profile.font_size:
            return self.font
        from epp import _get_font
        return _get_font(size)

    def _wrap(self, text, fmt):
        width_mult = max(1, fmt.get("width_mult", 1))
        max_cells = max(1, (self.profile.width_dots - 2 * self.profile.margin_dots)
                        // (self.profile.cell_width * width_mult))
        result = []
        for original in text.split("\n"):
            if original == "":
                result.append("")
                continue
            for start in range(0, len(original), max_cells):
                result.append(original[start:start + max_cells])
        return result

    def _measure_lines(self, block):
        text = self._clean_text(block["text"])
        fmt = self._effective_format(text, block["format"])
        font = self._font_for(fmt.get("height_mult", 1))
        lines = self._wrap(text, fmt)
        font_bbox = font.getbbox("Ag")
        glyph_height = max(1, font_bbox[3] - font_bbox[1])
        line_height = max(
            glyph_height + 2,
            int(self.profile.font_size * self.profile.line_spacing
                * max(1, fmt.get("height_mult", 1))),
        )
        return lines, fmt, font, line_height

    def _draw_glyph(self, canvas, x, y, char, font, cell_width, cell_height, bold):
        if char == " ":
            return
        bbox = font.getbbox(char)
        glyph_width = max(1, bbox[2] - bbox[0])
        glyph_height = max(1, bbox[3] - bbox[1])
        glyph = Image.new("1", (glyph_width + 4, glyph_height + 4), 1)
        glyph_draw = ImageDraw.Draw(glyph)
        glyph_draw.text(
            (2 - bbox[0], 2 - bbox[1]),
            char,
            font=font,
            fill=0,
            stroke_width=1 if bold else 0,
            stroke_fill=0 if bold else None,
        )
        # Keep one glyph per cell and prevent wide CJK glyphs from clipping.
        target_width = max(1, min(cell_width, glyph.width))
        if glyph.width > cell_width:
            glyph = glyph.resize((target_width, glyph.height), Image.Resampling.LANCZOS)
        draw_x = x + max(0, (cell_width - glyph.width) // 2)
        draw_y = y + max(0, (cell_height - glyph.height) // 2)
        canvas.paste(glyph, (draw_x, draw_y))

    def _draw_text_block(self, canvas, draw, y, block):
        lines, fmt, font, line_height = self._measure_lines(block)
        width_mult = max(1, fmt.get("width_mult", 1))
        cell_width = self.profile.cell_width * width_mult
        usable_width = self.profile.width_dots - 2 * self.profile.margin_dots
        for line in lines:
            rendered_width = min(usable_width, len(line) * cell_width)
            if fmt.get("align") == "center":
                x = (self.profile.width_dots - rendered_width) // 2
            elif fmt.get("align") == "right":
                x = self.profile.width_dots - self.profile.margin_dots - rendered_width
            else:
                x = self.profile.margin_dots
            x = max(self.profile.margin_dots, min(x, self.profile.width_dots - rendered_width))
            for index, char in enumerate(line):
                self._draw_glyph(
                    canvas,
                    x + index * cell_width,
                    y,
                    char,
                    font,
                    cell_width,
                    line_height,
                    bool(fmt.get("bold")),
                )
            if fmt.get("underline"):
                draw.line((x, y + line_height - 2,
                           min(self.profile.width_dots - 1, x + rendered_width),
                           y + line_height - 2), fill=0)
            y += line_height + self.profile.line_gap
        return y

    def _image(self, token):
        width_bytes = token.metadata["width_bytes"]
        height = token.metadata["height"]
        payload_size = width_bytes * height
        payload = token.raw[8:8 + payload_size]
        if len(payload) != payload_size:
            raise UnsupportedLegacyObject("truncated raster image")
        image = Image.new("1", (width_bytes * 8, height), 1)
        pixels = image.load()
        for row in range(height):
            for byte_index in range(width_bytes):
                byte_value = payload[row * width_bytes + byte_index]
                for bit in range(8):
                    pixels[byte_index * 8 + bit, row] = (
                        0 if byte_value & (0x80 >> bit) else 1
                    )

        if self.profile.image_invert:
            image = image.point(lambda value: 0 if value else 1)
        if self.profile.image_scale_to_width and image.width > self.profile.width_dots:
            scaled_height = max(1, round(image.height * self.profile.width_dots / image.width))
            image = image.resize(
                (self.profile.width_dots, scaled_height),
                Image.Resampling.NEAREST,
            )
        return image

    def _render_blocks(self, blocks):
        if self.font is None:
            raise ValueError("LegacyRasterRenderer requires a loaded font")
        if not blocks:
            return None

        # Measure blocks in source order.  Each block contributes its own
        # height; text and incoming raster images are never reordered.
        prepared = []
        total_height = 0
        for block in blocks:
            if block["kind"] == "text":
                lines, fmt, font, line_height = self._measure_lines(block)
                height = len(lines) * (line_height + self.profile.line_gap)
                prepared.append((block, height, None))
                total_height += height
            elif block["kind"] == "blank":
                height = max(1, int(self.profile.font_size * self.profile.line_spacing))
                prepared.append((block, height, None))
                total_height += height
            elif block["kind"] == "image":
                image = self._image(block["token"])
                prepared.append((block, image.height + self.profile.line_gap, image))
                total_height += image.height + self.profile.line_gap

        canvas = Image.new("1", (self.profile.width_dots, max(1, total_height)), 1)
        draw = ImageDraw.Draw(canvas)
        y = 0
        for block, height, image in prepared:
            if block["kind"] == "text":
                y = self._draw_text_block(canvas, draw, y, block)
            elif block["kind"] == "image":
                canvas.paste(image, (0, y))
                y += height
            else:
                y += height

        width_bytes = (self.profile.width_dots + 7) // 8
        pixels = canvas.load()
        bitmap = bytearray(width_bytes * canvas.height)
        for row in range(canvas.height):
            for column in range(self.profile.width_dots):
                if pixels[column, row] == 0:
                    bitmap[row * width_bytes + column // 8] |= 0x80 >> (column % 8)
        return width_bytes, canvas.height, bytes(bitmap)

    def render_pages(self):
        """Render ordered receipt pages, keeping each cut with its page."""
        self._flush()
        if self.blocks or not self.pages:
            self.pages.append({"blocks": self.blocks, "cut": None})
            self.blocks = []

        return [
            (self._render_blocks(page["blocks"]), page["cut"])
            for page in self.pages
        ]

    def render(self):
        """Render one page for compatibility with existing callers."""
        pages = self.render_pages()
        if len(pages) != 1:
            raise UnsupportedLegacyObject("multiple pages require render_pages()")
        return pages[0][0]


def render_legacy_receipts(source_data: bytes, font, profile: Optional[LegacyProfile] = None):
    renderer = LegacyRasterRenderer(profile, font)
    renderer.consume(parse_stream(source_data))
    return renderer.render_pages()


def render_legacy_receipt(source_data: bytes, font, profile: Optional[LegacyProfile] = None):
    pages = render_legacy_receipts(source_data, font, profile)
    if len(pages) != 1:
        raise UnsupportedLegacyObject("multiple pages require render_legacy_receipts()")
    return pages[0]
