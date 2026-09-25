import pathlib
import pytest
from gt7_result import GT7TestResult

from InkscapeHarness import (
    run_gt7_test,
    discover_svg_files
)

UNSUPPORTED_FEATURES_THRESHOLDS = { "*": 0.5  }

TEST_SUITE = pathlib.Path("assets/unsupported features")

cases, ids = discover_svg_files(TEST_SUITE)

@pytest.mark.parametrize("case_name, svg_path", cases, ids=ids)
def test_svg_case(case_name, svg_path, assets_root, request):
    try:
        result = run_gt7_test(TEST_SUITE.name, case_name, svg_path, assets_root, thresholds=UNSUPPORTED_FEATURES_THRESHOLDS)
    except Exception as e:
        # Attach a failure result so the HTML report still shows something
        request.node.gt7_result = GT7TestResult(
            score=0.0,
            threshold=0.0,
            input_svg=svg_path,
            output_svg="ERROR",
            input_png="ERROR",
            output_png="ERROR",
            diff_png="ERROR"
        )
        raise e
    request.node.gt7_result = result


