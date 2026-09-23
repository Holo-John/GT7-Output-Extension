import pathlib
import pytest

from InkscapeHarness import (
    run_gt7_test,
    discover_svg_files
)

GRADIENT_THRESHOLDS = { "pattern-with-gradient-chain.svg": 0.5  }

TEST_SUITE = pathlib.Path("assets/pattern")

cases, ids = discover_svg_files(TEST_SUITE)

@pytest.mark.parametrize("case_name, svg_path", cases, ids=ids)
def test_svg_case(case_name, svg_path, assets_root, request):
    result = run_gt7_test(TEST_SUITE.name, case_name, svg_path, assets_root, thresholds=GRADIENT_THRESHOLDS)
    request.node.gt7_result = result
