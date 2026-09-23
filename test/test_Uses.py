import pathlib
import pytest

from InkscapeHarness import (
    run_gt7_test,
    discover_svg_files,
    DEFAULT_PARAMS
)

TEST_SUITE = pathlib.Path("assets/uses")
OVERRIDE_PARAMS = {
    "compress_output": True
}

cases, ids = discover_svg_files(TEST_SUITE)

@pytest.mark.parametrize("case_name, svg_path", cases, ids=ids)
def test_svg_case(case_name, svg_path, assets_root, request):
    if case_name == "flag_of_brasil_well_formed":
        params = OVERRIDE_PARAMS
    else:
        params = DEFAULT_PARAMS

    result = run_gt7_test(TEST_SUITE.name, case_name, svg_path, assets_root, params=params)
    request.node.gt7_result = result
