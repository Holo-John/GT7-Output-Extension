from src.gt7_export import GT7Export
from test.InkscapeWrapper import run_extension_on_svg

import os
import tempfile
import subprocess
from skimage import io as skio
from skimage.metrics import structural_similarity as ssim
import numpy as np

from test.InkscapeWrapper import run_extension_on_svg, render_svg_to_png, compare_images
from src.gt7_export import GT7Export



def test_convert_svg_file(path="assets/gradients/Circle A.svg"):
    print(f"Converting file {path}...")

    # --- Clean temp directory before each run ---
    tmpdir = tempfile.mkdtemp()

    expected_svg = path
    actual_svg_path = os.path.join(tmpdir, "actual.svg")
    expected_png = os.path.join(tmpdir, "expected.png")
    actual_png = os.path.join(tmpdir, "actual.png")

    # --- Run your extension ---
    result = run_extension_on_svg(GT7Export, path)
    print(result)

    # --- Write actual SVG to temp file ---
    with open(actual_svg_path, "w", encoding="utf-8") as f:
        f.write(result)

    # --- Render both SVGs to PNG ---
    render_svg_to_png(expected_svg, expected_png)
    render_svg_to_png(actual_svg_path, actual_png)

    # --- Compare images ---
    score, diff = compare_images(expected_png, actual_png)
    print(f"SSIM score: {score}")

    # --- Assert similarity threshold ---
    assert score > 0.90, f"Images differ too much (SSIM={score})"

    # --- Cleanup ---
    try:
        os.remove(actual_svg_path)
        os.remove(expected_png)
        os.remove(actual_png)
        os.rmdir(tmpdir)
    except Exception:
        pass



if __name__ == "__main__":
    test_convert_svg_file()
