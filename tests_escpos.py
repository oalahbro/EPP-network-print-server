import json
from pathlib import Path

from escpos import decode_gs_character_size, parse_stream, replace_cuts
from epp import prepare_print_data, _get_font
from legacy_renderer import LegacyProfile, LegacyRasterRenderer, render_legacy_receipt

ROOT = Path(__file__).resolve().parent


def load_history():
    files = [ROOT / "print_history.json"] + sorted((ROOT / "history").glob("print_history_*.json"))
    entries = []
    for path in files:
        if path.exists():
            entries.extend(json.loads(path.read_text(encoding="utf-8")))
    return entries


def source_job(prefix):
    return next(item for item in load_history() if item["timestamp"].startswith(prefix))


def run_tests():
    tests = [value for name, value in globals().items()
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


def test_native_source_round_trips_exactly():
    source = bytes.fromhex(source_job("2026-08-27 00:22:02")["raw_data"])
    tokens = parse_stream(source)
    assert b"".join(token.raw for token in tokens) == source
    image = next(token for token in tokens if token.kind == "image")
    assert image.metadata == {"width_bytes": 60, "height": 320}


def test_native_mode_preserves_existing_image_job():
    source = bytes.fromhex(source_job("2026-08-27 00:22:02")["raw_data"])
    output, mode = prepare_print_data(source, {"PRINT_MODE": "native"})
    assert output == source
    assert mode == "native"


def test_hybrid_mode_does_not_rasterize_binary_image_pixels():
    source = bytes.fromhex(source_job("2026-08-27 00:22:02")["raw_data"])
    output, mode = prepare_print_data(source, {"PRINT_MODE": "hybrid"})
    assert output == source
    assert mode == "native"


def test_cut_replacement_does_not_touch_image_payload():
    source = bytes.fromhex(source_job("2026-08-27 00:22:02")["raw_data"])
    output = replace_cuts(source, "partial")
    image = next(token for token in parse_stream(output) if token.kind == "image")
    original = next(token for token in parse_stream(source) if token.kind == "image")
    assert image.raw == original.raw
    assert output.endswith(b"\x1d\x56\x41\x03")


def test_old_transformed_job_is_a_single_legacy_image():
    source = bytes.fromhex(source_job("2026-08-27 00:17:54")["raw_data"])
    images = [token for token in parse_stream(source) if token.kind == "image"]
    assert len(images) == 1
    assert images[0].metadata == {"width_bytes": 48, "height": 172}
    assert source.endswith(b"\x1d\x56\x42\x03")


def test_legacy_render_composes_source_without_raw_text():
    source = bytes.fromhex(source_job("2026-08-27 00:22:02")["raw_data"])
    output, mode = prepare_print_data(
        source,
        {"PRINT_MODE": "legacy_full_raster", "RASTER_MAX_WIDTH": 384,
         "RASTER_FONT_SIZE": 20},
    )
    tokens = parse_stream(output)
    assert mode == "legacy-full-raster"
    assert tokens[0].kind == "image"
    assert tokens[-1].kind == "cut"
    assert all(token.kind in ("image", "cut") for token in tokens)
    assert tokens[0].metadata["width_bytes"] == 48
    assert tokens[0].metadata["height"] > 0
    assert b"KITCHEN" not in output
    assert b"PRINTER TEST" not in output


def test_legacy_does_not_clip_unbroken_text():
    source = b"\x1b@\x1b!\x18#SMDC178775360690\n\x1dV\x42\x03"
    output, mode = prepare_print_data(
        source,
        {"PRINT_MODE": "legacy_full_raster", "RASTER_MAX_WIDTH": 384,
         "RASTER_FONT_SIZE": 20},
    )
    image = parse_stream(output)[0]
    assert mode == "legacy-full-raster"
    assert image.metadata["width_bytes"] == 48
    assert image.metadata["height"] > 0
    assert output.endswith(b"\x1d\x56\x42\x03")


def test_legacy_renderer_keeps_source_image_size_metadata():
    source = bytes.fromhex(source_job("2026-08-27 00:22:02")["raw_data"])
    original = next(token for token in parse_stream(source) if token.kind == "image")
    rendered, cut = render_legacy_receipt(source, _get_font(20))
    assert cut == b"\x1d\x56\x42\x03"
    assert rendered[0] == 48
    assert rendered[1] > 0
    assert len(original.raw) == 8 + 60 * 320


def test_gs_character_size_uses_producer_convention():
    assert decode_gs_character_size(0x00) == (1, 1)
    assert decode_gs_character_size(0x01) == (1, 1)
    assert decode_gs_character_size(0x10) == (1, 1)
    assert decode_gs_character_size(0x11) == (1, 1)
    assert decode_gs_character_size(0x23) == (3, 2)
    assert decode_gs_character_size(0x76) == (6, 7)


def test_gs_character_size_is_preserved_in_native_mode():
    source = b"\x1d\x21\x01X"
    output, mode = prepare_print_data(source, {"PRINT_MODE": "native"})
    assert output == source
    assert mode == "native"


def test_hybrid_special_text_uses_normal_producer_size():
    source = b"\x1d\x21\x01Korean \xa4\xa4\xa4\xbf\n"
    output, mode = prepare_print_data(
        source,
        {"PRINT_MODE": "hybrid", "RASTER_FONT_SIZE": 26,
         "RASTER_MAX_WIDTH": 384},
    )
    images = [token for token in parse_stream(output) if token.kind == "image"]
    assert mode == "hybrid"
    assert len(images) == 1
    assert images[0].metadata["height"] < 60


# The producer uses GS ! 0x01 as normal size; native mode remains exact.


def test_legacy_renderer_decodes_escpos_image_polarity():
    source = b"\x1dv0\x00\x01\x00\x01\x00\x80"
    token = next(token for token in parse_stream(source) if token.kind == "image")
    renderer = LegacyRasterRenderer(LegacyProfile(width_dots=8, columns=1), _get_font(16))
    image = renderer._image(token)
    assert image.getpixel((0, 0)) == 0
    assert image.getpixel((1, 0)) == 1


def test_legacy_renderer_preserves_multiple_cut_pages():
    """Each receipt is rasterized and cut before the next receipt begins."""
    source = b"A\n\x1dV\x42\x03B\n\x1dV\x42\x03"
    output, mode = prepare_print_data(
        source,
        {"PRINT_MODE": "legacy_full_raster", "RASTER_MAX_WIDTH": 192,
         "RASTER_FONT_SIZE": 12},
    )
    kinds = [token.kind for token in parse_stream(output)]
    assert mode == "legacy-full-raster"
    assert kinds == ["image", "cut", "image", "cut"]


if __name__ == "__main__":
    run_tests()
