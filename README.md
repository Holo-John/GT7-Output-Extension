# GT7 Output Extension - GT7OE

Inkscape output extension that converts SVG 2.0 artwork into SVG 1.1 files compatible with the Gran Turismo 7 Livery Editor.

## Quick User Guide

GT7OE is platform-independent and requires Inkscape 1.4 or later.

Installation consists of only a few steps:

1. [Download the Latest Release](https://github.com/Holo-John/GT7-Output-Extension/releases/latest)
2. Extract the contents of the `gt7_output_extension.zip` archive.
3. Open Inkscape and navigate to **Preferences**.
4. Click the **Open** button next to **User Extensions**. This will open the user extensions directory in your operating system's file manager.
5. Copy `gt7_output.inx` and `gt7_output.py` into the user extensions directory.

Further usage instructions, feature documentation, and practical examples are included in the user manual contained in the release ZIP archive.

## Quick Development Guide

If you want to contribute to GT7OE or build it from source, follow these steps to set up a local development environment:

### Prerequisites

- Inkscape 1.4.2 or later
- Python
- Git
- Visual Studio Code
- GnuPG (only for official releases)

### Setup

1. Clone the repository.
2. Copy `.env.example` to `.env` and follow the instructions inside the file to configure the required paths.
3. Open the repository in Visual Studio Code.
4. Create the development environment:
   - Press `CTRL+SHIFT+P`
   - Select **Tasks: Run Task**
   - Select **GT7: Setup .venv**

This creates a `.venv` folder containing the Python environment required for testing and Visual Studio Code tasks. All modules listed in `requirements.txt` are automatically installed.

### Other Available Tasks

- **GT7: Deploy** → Deploys the extension into the local Inkscape installation.
- **GT7: Package** → Builds an unsigned ZIP archive in the `dist/build` folder containing the extension, license, and user manual.
- **GT7: Release** → Builds a signed ZIP archive and signature file in the `dist/build` folder containing the extension, license, and user manual.
- **GT7: API Docs** → Generates API documentation in `docs/build`.
