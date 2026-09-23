# GT7-Output-Extension
Inkscape output extension that converts SVG 2.0 content to SVG 1.1 considering additional constraints of the Gran Turismo 7 Livery Editor.

If you want to clone this repository, follow these steps to setup your devenv:

1. Install Prerequisites
   - Inkscape version >= 1.4.2
   - Python
   - Git
   - Visual Studio Code

2. Checkout the repository to a local folder

3. Go to your local folder, and copy ".env.example" to ".env". Follow the instructions inside the file to enter necessary paths.

4. Open your local folder in Visual Studio Code

5. Setup the .venv environment required for testing:
   - In Visual Studio Code, press <CTRL>+<SHIFT>+<P>
   - Enter Tasks: Run Task (follow autocompletion)
   - Enter GT7: Setup .venv (follow autocompletion)

   This will create a .venv folder with a python environment needed for running tests and Visual Studio Code tasks. The python modules listed in requirements.txt will automatically be "pip-installed" into the environment.

6. Other pre-tasks defined in the workspace are:
   - GT7: Deploy -> Deploys the GT7 Output Extensions into the local Inkscape installation.
   - GT7: Release -> Build a ZIP archive in the dist folder containing the deployable files, license, and user manual.
   - GT7: API Docs -> Generates API documentation into the folder docs/build
