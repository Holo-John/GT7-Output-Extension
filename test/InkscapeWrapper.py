import io
import os
import tempfile
import subprocess
import inkex
from skimage import io as skio
from skimage.metrics import structural_similarity as ssim
import numpy as np
import numpy.typing as npt
from typing import cast, Tuple
import matplotlib.pyplot as plt

INKSCAPE = r"C:\Program Files\Inkscape\bin\inkscape.exe"

# Ensure Inkex uses the same executable when it shells out to Inkscape.
os.environ.setdefault("INKSCAPE_EXE", INKSCAPE)

try:
    import inkex.command as inkex_command
    inkex_command.INKSCAPE_EXECUTABLE_NAME = INKSCAPE
except Exception:
    pass

def run_extension_on_svg(extension_class, svg_path):
    """
    Loads an SVG file, runs an OutputExtension, and returns the output SVG as a string.
    """

    # 1. Load SVG
    doc = inkex.load_svg(svg_path)

    # 2. Create extension instance
    ext = extension_class()

    # 3. Attach document + root
    ext.document = doc
    ext.svg = doc.getroot()

    # 4. Create tempdir (Inkex always does this)
    ext.tempdir = tempfile.mkdtemp()

    # 5. Prepare output stream
    output_stream = io.StringIO()

    # 6. Run save() directly (OutputExtensions do not use effect())
    ext.save(output_stream)

    # 7. Return SVG text
    return output_stream.getvalue()



def render_svg_to_png(svg_path, png_path):
    subprocess.run([
        INKSCAPE,
        "--export-type=png",
        f"--export-filename={png_path}",
        svg_path
    ], check=True)


def compare_images(img1_path: str, img2_path: str) -> Tuple[float, npt.NDArray[np.float64]]:
    img1 = skio.imread(img1_path)
    img2 = skio.imread(img2_path)

    img1_gray = np.mean(img1, axis=2)
    img2_gray = np.mean(img2, axis=2)

    # SSIM requires explicit data_range for float images
    data_range = max(img1_gray.max(), img2_gray.max()) - min(img1_gray.min(), img2_gray.min())

    result = ssim(img1_gray, img2_gray, full=True, data_range=data_range)

    # Handle both possible return shapes
    if isinstance(result, tuple) and len(result) == 3:
        score, diff, _ = result
    else:
        score, diff = result # type: ignore

    return score, diff



def save_diff_image(diff, path):
    plt.imsave(path, diff, cmap="gray")


def assert_gt7_compliant_file(filename):
    """
    Load an SVG file from disk and assert GT7 compliance.
    Delegates to assert_gt7_compliant(svg_root).
    """
    import inkex

    # Load SVG document
    doc = inkex.load_svg(filename)
    root = doc.getroot()

    # Delegate to your existing compliance checker
    return assert_gt7_compliant_svg_tree(root)



def assert_gt7_compliant_svg_tree(svg_root):
    """
    Assert that the SVG DOM contains no GT7-forbidden constructs.
    Raises AssertionError with a precise message on violation.
    """

    forbidden_tags = {
        "filter",
        "mask",
        "pattern",
        "linearGradient",
        "radialGradient",
        "clipPath",
        "marker",
        "metadata",
        "foreignObject",
        "symbol",
        "use",
    }

    forbidden_attributes = {
        "filter",
        "mask",
        "clip-path",
        "marker-start",
        "marker-mid",
        "marker-end",
        "paint-order",
        "aria-label",
        "text-anchor",
        "vector-effect",
        "mix-blend-mode",
    }

    # 1. Check forbidden tags anywhere in the DOM
    for el in svg_root.iter():
        tag = el.tag.split("}")[-1]  # strip namespace
        if tag in forbidden_tags:
            raise AssertionError(f"GT7 violation: forbidden tag <{tag}> found")

        # 2. Check forbidden attributes
        for attr in forbidden_attributes:
            if attr in el.attrib:
                raise AssertionError(
                    f"GT7 violation: forbidden attribute '{attr}' on <{tag}>"
                )

        # 3. Check style-based forbidden references
        style = el.attrib.get("style", "")
        if "filter:" in style:
            raise AssertionError("GT7 violation: filter reference inside style")
        if "mask:" in style:
            raise AssertionError("GT7 violation: mask reference inside style")
        if "clip-path:" in style:
            raise AssertionError("GT7 violation: clip-path reference inside style")
        if "marker-" in style:
            raise AssertionError("GT7 violation: marker reference inside style")

    # 4. Check <defs> contains no forbidden children
    defs = svg_root.find(".//{http://www.w3.org/2000/svg}defs")
    if defs is not None:
        for child in defs:
            tag = child.tag.split("}")[-1]
            if tag in forbidden_tags:
                raise AssertionError(
                    f"GT7 violation: forbidden <{tag}> found inside <defs>"
                )

    # 5. Check no <style> blocks exist
    style_blocks = svg_root.findall(".//{http://www.w3.org/2000/svg}style")
    if style_blocks:
        raise AssertionError("GT7 violation: <style> blocks are not allowed")

    # 6. Check no embedded scripts
    script_blocks = svg_root.findall(".//{http://www.w3.org/2000/svg}script")
    if script_blocks:
        raise AssertionError("GT7 violation: <script> blocks are not allowed")

    # If we reach here, the file is compliant
    return True


