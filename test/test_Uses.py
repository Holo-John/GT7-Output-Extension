import pathlib
import tempfile
import os
import pytest

from test.InkscapeWrapper import (
    run_extension_on_svg,
    render_svg_to_png,
    compare_images
)
from src.gt7_export import GT7Export


GRADIENT_SUITE = pathlib.Path("assets/uses")


def discover_svg_files():
    """Yield (case_name, svg_path) for each SVG file in the suite directory."""
    for svg_file in GRADIENT_SUITE.glob("*.svg"):
        yield svg_file.stem, svg_file


@pytest.mark.parametrize("case_name, svg_path", list(discover_svg_files()))
def test_uses_case(case_name, svg_path):
    print(f"\n=== Running gradient test case: {case_name} ===")

    tmpdir = tempfile.mkdtemp()

    actual_svg = pathlib.Path(tmpdir) / "actual.svg"
    expected_png = pathlib.Path(tmpdir) / "expected.png"
    actual_png = pathlib.Path(tmpdir) / "actual.png"

    # Run GT7 exporter
    result = run_extension_on_svg(GT7Export, svg_path)

    # Write actual SVG
    actual_svg.write_text(result, encoding="utf-8")

    # Render expected + actual
    render_svg_to_png(str(svg_path), str(expected_png))
    render_svg_to_png(str(actual_svg), str(actual_png))

    # Compare
    score, diff = compare_images(str(expected_png), str(actual_png))
    print(f"SSIM score for {case_name}: {score}")

    # Threshold
    assert score > 0.90, f"Gradient test '{case_name}' failed (SSIM={score})"

    # Cleanup
    try:
        os.remove(actual_svg)
        os.remove(expected_png)
        os.remove(actual_png)
        os.rmdir(tmpdir)
    except Exception:
        pass
