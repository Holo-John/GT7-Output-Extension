import io
import inkex
import tempfile
import subprocess
import inkex
import tempfile
import io
from skimage import io as skio
from skimage.metrics import structural_similarity as ssim
import numpy as np
import numpy.typing as npt
from typing import cast, Tuple
import matplotlib.pyplot as plt

INKSCAPE = r"C:\Program Files\Inkscape\bin\inkscape.exe"

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

