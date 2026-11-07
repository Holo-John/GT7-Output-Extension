import inkex

from gt7_output import GT7Output


def test_sweep_rounds_compact_path_only_after_structural_cleanup():
    svg = inkex.etree.fromstring(
        b"""<svg xmlns="http://www.w3.org/2000/svg">
          <path id="p" d="m 59.008241,19.174376 c 2.241564,-2.562406 5.761804,-3.628872 9.069834,-2.562406" />
          <metadata />
        </svg>"""
    )
    extension = GT7Output()
    extension.parse_arguments(
        ["--compress_output", "true", "--rounding_precision", "3"]
    )
    extension.svg = svg

    extension.sweep_svg_tree()

    path = svg.xpath('//*[local-name()="path"]')[0]
    assert path.get("d") == "m59.008,19.174 c2.242,-2.562 5.762,-3.629 9.07,-2.562"
    assert not svg.xpath('//*[local-name()="metadata"]')
