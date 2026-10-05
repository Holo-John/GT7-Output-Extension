import pathlib
import xml.etree.ElementTree as ET
import pytest

from InkscapeHarness import (
    run_gt7_test,
    discover_svg_files
)
from gt7_output import GT7Output

TEST_SUITE = pathlib.Path(__file__).resolve().parents[1] / "assets" / "mix-blend-mode"

BLEND_MODE_THRESHOLDS = { "*": 0.8  }

if not TEST_SUITE.exists():
    pytest.skip(f"Missing asset suite: {TEST_SUITE}", allow_module_level=True)

cases, ids = discover_svg_files(TEST_SUITE)

@pytest.mark.parametrize("case_name, svg_path", cases, ids=ids)
def test_svg_case(case_name, svg_path, assets_root, request):
    result = run_gt7_test(TEST_SUITE.name, case_name, svg_path, assets_root, thresholds=BLEND_MODE_THRESHOLDS)
    request.node.gt7_result = result

    if case_name == "multiple-gradients":
        output_root = ET.parse(result.output_svg).getroot()
        gradients = {
            gradient.get("id"): gradient.tag.rsplit("}", 1)[-1]
            for gradient in output_root.iter()
            if gradient.tag.rsplit("}", 1)[-1] in {"linearGradient", "radialGradient"}
        }
        assert any(
            gradients.get(path.get("fill", "").removeprefix("url(#").removesuffix(")")) == "radialGradient"
            for path in output_root.iter()
            if path.tag.rsplit("}", 1)[-1] == "path"
        ), "Expected radial overlay gradient to be retained for an overlap path"


@pytest.mark.parametrize(
    ("mode", "source", "backdrop", "expected"),
    [
        ("color-burn", 0.5, 0.75, 0.5),
        ("color-burn", 0.0, 0.75, 0.0),
        ("color-burn", 0.0, 1.0, 1.0),
        ("color-dodge", 0.5, 0.25, 0.5),
        ("color-dodge", 1.0, 0.25, 1.0),
        ("color-dodge", 1.0, 0.0, 0.0),
    ],
)
def test_color_burn_and_dodge_channels(mode, source, backdrop, expected):
    result = GT7Output()._blend_rgba(
        (source, source, source, 1.0),
        (backdrop, backdrop, backdrop, 1.0),
        mode,
    )

    assert result == pytest.approx((expected, expected, expected, 1.0))


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("color-burn", (1.0, 0.125, 0.0, 1.0)),
        ("color-dodge", (1.0, 1.0, 0.0, 1.0)),
    ],
)
def test_color_burn_and_dodge_blue_over_orange(mode, expected):
    blue = (0.0, 136 / 255, 1.0, 1.0)
    orange = (1.0, 136 / 255, 0.0, 1.0)

    result = GT7Output()._blend_rgba(blue, orange, mode)

    assert result == pytest.approx(expected)