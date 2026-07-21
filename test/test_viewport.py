import pathlib
import pytest

from InkscapeHarness import (
    run_gt7_test
)

TEST_SUITE = pathlib.Path("assets/viewport")

def discover_svg_files():
    """Yield (case_name, svg_path) for each SVG file in the suite directory."""
    for svg_file in TEST_SUITE.glob("*.svg"):
        yield svg_file.stem, svg_file


@pytest.mark.parametrize("case_name, svg_path", list(discover_svg_files()))
def test_svg_case(case_name, svg_path):
    run_gt7_test(TEST_SUITE.name, case_name, svg_path)