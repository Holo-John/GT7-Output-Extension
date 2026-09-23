

def pytest_html_results_table_header(cells):
    cells.insert(2, "<th>SSIM</th>")
    cells.insert(3, "<th>Preview</th>")

def pytest_html_results_table_row(report, cells):
    result = getattr(report, "gt7_result", None)
    if result is None:
        return

    # SSIM column
    cells.insert(2, f"<td>{result.score:.4f} / {result.threshold:.4f}</td>")

    # Preview column
    preview = f"""
        <td>
            <div style='display:flex; gap:20px; align-items:flex-start;'>

                <div>
                    <strong>Input SVG</strong><br>
                    <object type='image/svg+xml' data='{result.input_svg}'
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
    cells.insert(3, preview)



    # Preview column
    preview = f"""
        <td>
            <div style='display:flex; gap:20px;'>
                <div>
                    <strong>Input</strong><br>
                    <object type='image/svg+xml' data='{result.input_svg}'></object>
                </div>
                <div>
                    <strong>Output</strong><br>
                    <object type='image/svg+xml' data='{result.output_svg}'></object>
                </div>
                <div>
                    <strong>Diff</strong><br>
                    <img src='{result.diff_png}' style='max-width:200px;'/>
                </div>
            </div>
        </td>
    """

    cells.insert(3, preview)


