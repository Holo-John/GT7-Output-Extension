# GT7-Output-Extension
Inkscape output extension that converts SVG 2.0 content to SVG 1.1 considering additional constraints of the Gran Turismo 7 Livery Editor.

If you want to modify the code, follow these steps to setup your devenv:

1. Install Prerequisites
   - Inkscape version >= 1.4.2
   - Python
   - Git
   - Visual Studio Code

2. Checkout the repository to a local folder

3. Open your local folder in Visual Studio Code

4. Setup the .venv environment required for testing:
   - In Visual Studio Code, press <CTRL>+<SHIFT>+<P>
   - Enter Tasks: Run Task (follow autocompletion)
   - Enter GT7: Setup .venv (follow autocompletion)

   This will create a .venv folder with a python environment needed for running tests and Visual Studio Code tasks. The python modules listed in requirements.txt will be pip installed automatically into the environment.
