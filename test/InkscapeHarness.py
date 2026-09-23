from gt7_output import GT7Output
import io
import os
import tempfile
import subprocess
import inkex
from dotenv import load_dotenv
from skimage import io as skio
from skimage.metrics import structural_similarity as ssim
import numpy as np
import numpy.typing as npt
from typing import Tuple
import matplotlib.pyplot as plt
import sys
import inspect
import shutil
import pathlib
from gt7_result import GT7TestResult

load_dotenv()
INKSCAPE:str = os.environ["INKSCAPE_EXE"]


# Ensure Inkex uses the same executable when it shells out to Inkscape.
os.environ.setdefault("INKSCAPE_EXE", INKSCAPE)

try:
    import inkex.command as inkex_command
    inkex_command.INKSCAPE_EXECUTABLE_NAME = INKSCAPE
except Exception:
    pass

def run_extension_on_svg(extension_class, svg_path, artifact_dir, params):
    """
    Loads an SVG file, runs an OutputExtension, and returns the output SVG as a string.
    """

    print("=== HARNESS INKEX DIAGNOSTICS ===")
    print("Python executable:", sys.executable)
    print("inkex loaded from:", inspect.getfile(inkex))
    print("inkex version:", getattr(inkex, "__version__", "NO VERSION ATTRIBUTE"))
    print("inkex has Element:", hasattr(inkex, "Element"))
    print("==================================")

    strip_alpha = params.get("strip_alpha", DEFAULT_PARAMS["strip_alpha"])
    rounding_precision = params.get("rounding_precision", DEFAULT_PARAMS["rounding_precision"])
    mesh_divisions = params.get("mesh_divisions", DEFAULT_PARAMS["mesh_divisions"])
    compress_output = params.get("compress_output", DEFAULT_PARAMS["compress_output"])
    log_level = params.get("log_level", DEFAULT_PARAMS["log_level"])

    # 1. Load SVG
    doc = inkex.load_svg(svg_path)

    # 2. Create extension instance
    ext = extension_class()

    if hasattr(ext, "test_dir"):
        ext.test_dir = artifact_dir  # pytest fixture

    #3. Set options
    test_args = [
        "--strip_alpha", f"{strip_alpha}",
        "--rounding_precision", f"{rounding_precision}",
        "--mesh_divisions", f"{mesh_divisions}",
        "--compress_output", f"{compress_output}",
        "--compress_output", f"{log_level}"
    ]
    ext.parse_arguments(test_args)

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


def render_svg_to_png(svg_path: str, png_path: str) -> None:
    subprocess.run([
        INKSCAPE,
        "--export-type=png",
        f"--export-filename={png_path}",
        svg_path
    ], check=True)


def compare_images(img1_path: str, img2_path: str) -> Tuple[float, npt.NDArray[np.float64]]:
    img1 = skio.imread(img1_path)
    img2 = skio.imread(img2_path)

    img1_gray = np.mean(img1, axis=2, keepdims=False)
    img2_gray = np.mean(img2, axis=2, keepdims=False)

    # SSIM requires explicit data_range for float images
    data_range = max(img1_gray.max(), img2_gray.max()) - min(img1_gray.min(), img2_gray.min())
    if data_range == 0:
        data_range = 1.0  # Avoid division by zero for uniform images   

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
        "clipPath",
        "marker",
        "metadata",
        "foreignObject",
        "symbol",
        "use",
        "meshgradient",
        "meshGradient",
        "meshrow",
        "meshRow",
        "meshpatch",
        "meshPatch"
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

SSIM_DEFAULT_THRESHOLDS = {
    "*": 0.90,
}

DEFAULT_PARAMS = {
    "compress_output": False,
    "strip_alpha": True, 
    "rounding_precision": 3, 
    "mesh_divisions": 4,
    "log_level": "DEBUG"
}

def resolve_threshold(svg_path: pathlib.Path, thresholds: dict[str, float]) -> float:
    # 1. Specific file override
    if svg_path.name in thresholds:
        return thresholds[svg_path.name]

    # 2. "*" default override
    if "*" in thresholds:
        return thresholds["*"]

    # 3. Hardcoded fallback
    return 0.9

def discover_svg_files(test_suite):
    files = list(test_suite.glob("*.svg"))
    cases = []
    ids = []

    for _, svg_file in enumerate(files, start=1):
        case_name = svg_file.stem
        cases.append((case_name, svg_file))
        ids.append(case_name)   # your custom ID

    return cases, ids


def run_gt7_test(test_source: str, case_name: str, svg_path: pathlib.Path, asset_root: pathlib.Path, thresholds: dict = SSIM_DEFAULT_THRESHOLDS, params: dict = DEFAULT_PARAMS):
    if asset_root is None:
        raise ValueError("asset_root must be provided")
    
    artifact_dir = asset_root / test_source / case_name
    artifact_dir.mkdir(parents=True, exist_ok=True)

    output_gt7_svg = artifact_dir / svg_path.with_suffix(".gt7.svg").name
    input_png      = artifact_dir / "input.png"
    output_png     = artifact_dir / "output.gt7.png"
    diff_png       = artifact_dir / "diff.png"    

    try:
        # Run GT7 exporter
        result = run_extension_on_svg(GT7Output, svg_path, artifact_dir, params)
        output_gt7_svg.write_text(result, encoding="utf-8")

    finally:
        # Copy logs + input SVG
        log_file = pathlib.Path(tempfile.gettempdir()) / "gt7_output_extension.log"
        shutil.copy(str(log_file), artifact_dir / svg_path.with_suffix(".log").name)
        shutil.copy(str(svg_path), artifact_dir / svg_path.name)

    # Render expected + actual
    render_svg_to_png(str(svg_path), str(input_png))
    render_svg_to_png(str(output_gt7_svg), str(output_png))

    # Compare
    score, diff = compare_images(str(input_png), str(output_png))
    print(f"SSIM score for {case_name}: {score}")

    save_diff_image(diff, diff_png)

    threshold = resolve_threshold(svg_path, thresholds)
    if score <= threshold:
        raise AssertionError(f"Gradient test '{case_name}' failed (SSIM={score})")

    assert_gt7_compliant_file(output_gt7_svg)

    return GT7TestResult(
        score,
        threshold,
        svg_path,
        output_gt7_svg,
        input_png,
        output_png,
        diff_png
    )


