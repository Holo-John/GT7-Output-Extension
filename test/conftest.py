"""
Pytest configuration and fixtures.
Ensures the project root is on sys.path for imports to work correctly.
"""
import sys
import os
import pathlib
import shutil
import pytest

# Add project root to Python path so 'src' and 'test' modules can be imported
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_OUTPUT_ROOT = PROJECT_ROOT / "test_output"

REPORT_FILE = TEST_OUTPUT_ROOT / "report.html"

def pytest_configure(config):
    # Only set the HTML path if the plugin is active
    if config.pluginmanager.hasplugin("html"):
        config.option.htmlpath = REPORT_FILE
        config.option.self_contained_html = True


# Add test directory to Python path so test modules like InkscapeHarness can be imported
test_dir = os.path.dirname(os.path.abspath(__file__))
if test_dir not in sys.path:
    sys.path.insert(0, test_dir)

@pytest.fixture(scope="session", autouse=True)
def _clean_dist():
    root = pathlib.Path(TEST_OUTPUT_ROOT)
    root.mkdir(parents=True, exist_ok=True)

    # Remove contents only
    for item in root.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item, ignore_errors=True)

    return root


@pytest.fixture(scope="module")
def suite_output(request):
    suite_name = request.module.TEST_SUITE.name
    suite_dir = pathlib.Path(TEST_OUTPUT_ROOT) / suite_name
    suite_dir.mkdir(parents=True, exist_ok=True)
    return suite_dir

@pytest.fixture
def case_output(suite_output, request):
    case_dir = suite_output / request.node.name
    case_dir.mkdir(exist_ok=True)
    return case_dir

@pytest.fixture(scope="session")
def assets_root():
    root = pathlib.Path(TEST_OUTPUT_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # 1. Let the default pytest runner or other plugins execute first
    outcome = yield

    # 2. Extract the actual TestReport object from the outcome
    report = outcome.get_result()

    # 3. Only process the actual test execution phase (skip setup and teardown)
    if report.when == "call":
        # Debug print to verify it works
        
        # Retrieve your custom attribute safely from the test item
        result = getattr(item, "gt7_result", None)
        if result is not None:
            # Attach your custom data directly to the report object
            report.gt7_result = result


def pytest_html_results_table_header(cells):
    """v4.x compliant header mutation."""
    # In v4, cells is a list of strings/HTML elements. 
    # Use standard string/HTML insertions instead of py.xml objects.
    cells.insert(2, "<th>SSIM</th>")
    cells.insert(3, "<th>Preview</th>")


def pytest_html_results_table_row(report, cells):
    """v4.x compliant row mutation."""
    # Always protect against setup/teardown phases
    if report.when != "call":
        return

    result = getattr(report, "gt7_result", None)
    if result is None:
        # Provide fallback empty cells so the row columns don't shift left
        cells.insert(2, "<td>-</td>")
        cells.insert(3, "<td>-</td>")
        return

    # 1. Format SSIM cell
    ssim_html = f"<td>{result.score:.4f} / {result.threshold:.4f}</td>"
    cells.insert(2, ssim_html)

    input_svg = result.output_svg.parent / result.input_svg.name

    # 2. Format complex Preview cell using standard multi-line strings
    preview_html = f"""
    <td>
        <div style='display:flex; gap:20px; align-items:flex-start;'>
            <div>
                <strong>Input SVG</strong><br>
                <object type='image/svg+xml' data='{input_svg}'
                        style='width:200px; border:1px solid #ccc;'></object>
            </div>
            <div>
                <strong>Generated SVG</strong><br>
                <object type='image/svg+xml' data='{result.output_svg}'
                        style='width:200px; border:1px solid #ccc;'></object>
            </div>
            <div>
                <strong>Diff PNG</strong><br>
                <img src='{result.diff_png}'
                     style='width:200px; border:1px solid #ccc;'/>
            </div>
        </div>
    </td>
    """
    cells.insert(3, preview_html)










