import pathlib
import pytest

from InkscapeHarness import (
    run_gt7_test,
    discover_svg_files
)

TEST_SUITE = pathlib.Path(__file__).resolve().parents[1] / "assets" / "meshGradients"

MESH_GRADIENT_THRESHOLDS = { "*": 0.8  }

if not TEST_SUITE.exists():
    pytest.skip(f"Missing asset suite: {TEST_SUITE}", allow_module_level=True)

cases, ids = discover_svg_files(TEST_SUITE)

@pytest.mark.parametrize("case_name, svg_path", cases, ids=ids)
def test_svg_case(case_name, svg_path, assets_root, request):
    result = run_gt7_test(TEST_SUITE.name, case_name, svg_path, assets_root, thresholds=MESH_GRADIENT_THRESHOLDS)
    request.node.gt7_result = result


