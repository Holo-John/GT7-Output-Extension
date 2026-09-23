import builtins
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import build


@pytest.fixture
def temp_workspace(tmp_path):
    workspace = tmp_path
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "gt7_output.py").write_text("print('py')\n", encoding="utf-8")
    (source_dir / "gt7_output.inx").write_text("<inkscape-extension />\n", encoding="utf-8")
    (workspace / "license.txt").write_text("license\n", encoding="utf-8")
    manual_dir = workspace / "manual"
    manual_dir.mkdir()
    (manual_dir / "manual.html").write_text("<html>Manual</html>\n", encoding="utf-8")
    dist_dir = workspace / "dist"
    dist_dir.mkdir()
    return workspace, source_dir, dist_dir


def test_build_release_bundle_creates_zip(temp_workspace):
    workspace, _, dist_dir = temp_workspace
    archive_path = build.build_release_bundle(workspace, dist_dir)

    assert archive_path.exists()
    assert archive_path.suffix == ".zip"
    assert archive_path.name.startswith("gt7_exporter")

    extracted = archive_path.parent / "bundle_extract"
    shutil.unpack_archive(archive_path, extracted)
    assert (extracted / "gt7_output.py").exists()
    assert (extracted / "gt7_output.inx").exists()
    assert (extracted / "license.txt").exists()
    assert (extracted / "manual" / "manual.html").exists()


def test_build_release_bundle_accepts_verbose_flag(temp_workspace):
    workspace, _, dist_dir = temp_workspace

    archive_path = build.build_release_bundle(workspace, dist_dir, verbose=True)

    assert archive_path.exists()


def test_deploy_extension_files_copies_to_target_dir(temp_workspace):
    workspace, _, _ = temp_workspace
    target_dir = temp_workspace[0] / "extensions"
    target_dir.mkdir()

    build.install_extension_files(workspace, target_dir)

    assert (target_dir / "gt7_output.py").read_text(encoding="utf-8") == "print('py')\n"
    assert (target_dir / "gt7_output.inx").read_text(encoding="utf-8") == "<inkscape-extension />\n"


def test_deploy_extension_files_requires_env_target_when_not_explicit(monkeypatch, tmp_path):
    workspace = tmp_path
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "gt7_output.py").write_text("print('py')\n", encoding="utf-8")
    (source_dir / "gt7_output.inx").write_text("<inkscape-extension />\n", encoding="utf-8")
    monkeypatch.setenv("INKSCAPE_EXTENSIONS_DIR", "   ")

    with pytest.raises(ValueError, match="INKSCAPE_EXTENSIONS_DIR"):
        build.install_extension_files(workspace)


def test_load_environment_reads_dotenv_file_without_python_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("INKSCAPE_EXTENSIONS_DIR=C:/temp/extensions\n", encoding="utf-8")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.delenv("INKSCAPE_EXTENSIONS_DIR", raising=False)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "dotenv":
            raise ModuleNotFoundError("No module named 'dotenv'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    build.load_environment()

    assert os.environ["INKSCAPE_EXTENSIONS_DIR"] == "C:/temp/extensions"


def test_build_module_imports_without_dotenv(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "dotenv":
            raise ModuleNotFoundError("No module named 'dotenv'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    spec = importlib.util.spec_from_file_location(
        "build_without_dotenv",
        Path(__file__).resolve().parents[1] / "build.py",
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert hasattr(module, "setup_environment")


def test_setup_environment_creates_venv_and_installs_requirements(tmp_path):
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("pytest>=9.0.0\n", encoding="utf-8")

    python_path = build.setup_environment(
        workspace_dir=tmp_path,
        venv_dir=tmp_path / ".venv",
        requirements_file=requirements_file,
    )

    assert python_path.exists()
    result = subprocess.run(
        [str(python_path), "-m", "pip", "show", "pytest"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_setup_environment_recreates_existing_venv(tmp_path):
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("pytest>=9.0.0\n", encoding="utf-8")
    venv_dir = tmp_path / ".venv"

    build.setup_environment(workspace_dir=tmp_path, venv_dir=venv_dir, requirements_file=requirements_file)
    marker = venv_dir / "marker.txt"
    marker.write_text("stale\n", encoding="utf-8")

    build.setup_environment(workspace_dir=tmp_path, venv_dir=venv_dir, requirements_file=requirements_file, recreate=True)

    assert not marker.exists()
    python_path = venv_dir / ("Scripts/python.exe" if __import__("os").name == "nt" else "bin/python")
    assert python_path.exists()
