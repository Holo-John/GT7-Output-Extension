from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
DIST_DIR = ROOT / "dist/build"
FILES_TO_INSTALL = ("gt7_output.py", "gt7_output.inx")
FILES_TO_BUNDLE = FILES_TO_INSTALL + ("LICENSE", "manual")

from pathlib import Path
import subprocess

def _load_environment() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def _clean_dist(
    output_dir: str | Path = DIST_DIR,
    verbose: bool = False,
) -> None:
    output_dir = Path(output_dir)

    if not output_dir.exists():
        return

    patterns = (
        "*.zip",
        "*.sig",
    )

    for pattern in patterns:
        for artifact in output_dir.glob(pattern):
            artifact.unlink()

            if verbose:
                print(f"  delete: {artifact.name}")


def _get_version(release:bool=False) -> str:
    
    try:
        git_exe = os.environ.get("GIT_EXE", "git")
        
        version = "def"

        if release:
            version = subprocess.check_output(
                [git_exe, "describe", "--tags"],
                text=True,
                cwd=ROOT,
            ).strip()

            # v1.0.1-7-g588beab -> v1.0.1
            version = version.split("-", 1)[0]

            # v1.0.1 -> 1.0.1
            version = version.removeprefix("v")

    except Exception as ex:
        print(f"Unable to determine version: {ex}")
        version = "dev"

    finally:
        return version
    

def _bundle_files(
    workspace_dir: str | Path = ROOT,
    source_dir: str | Path | None = None,
    output_dir: str | Path = DIST_DIR,
    verbose: bool = False,
    release:bool=False
) -> Path:
    workspace_dir = Path(workspace_dir)
    if source_dir is None:
        source_dir = workspace_dir / "src"
    source_dir = Path(source_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    version = _get_version(release)
    bundle_file = f"gt7_output_extension-{version}.zip"

    archive_path = output_dir / bundle_file
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_name in FILES_TO_BUNDLE:
            source_path = workspace_dir / relative_name
            if relative_name in FILES_TO_INSTALL:
                source_path = source_dir / relative_name
            if not source_path.exists():
                raise FileNotFoundError(f"Required bundle item not found: {source_path}")

            if source_path.is_dir():
                for nested_path in sorted(source_path.rglob("*")):
                    if nested_path.is_file():
                        archive.write(nested_path, arcname=str(nested_path.relative_to(workspace_dir)))
                        if verbose:
                            print(f"  zip: {nested_path.relative_to(workspace_dir)}")
            else:
                archive.write(source_path, arcname=relative_name)
                if verbose:
                    print(f"  zip: {relative_name}")

    if verbose:
        print(f"Created bundle: {archive_path}")
    return archive_path

def _sign_release(zip_file: str | Path, verbose: bool = False) -> Path:
    zip_file = Path(zip_file)

    if not zip_file.exists():
        raise FileNotFoundError(zip_file)

    gpg_exe = Path(os.environ["GPG_EXE"])

    if not gpg_exe.exists():
        raise FileNotFoundError(
            f"GPG executable not found: {gpg_exe}"
        )

    sig_file = zip_file.with_suffix(".sig")

    subprocess.run(
        [
            str(gpg_exe),
            "--output",
            str(sig_file),
            "--detach-sign",
            str(zip_file),
        ],
        check=True,
    )

    if verbose:
        print(f"Using GPG: {gpg_exe}")
        print(f"Created signature: {sig_file}")

    return sig_file


def deploy(
    workspace_dir: str | Path = ROOT,
    target_dir: str | Path | None = None,
    source_dir: str | Path | None = None,
    verbose: bool = False,
) -> Path:
    _load_environment()
    workspace_dir = Path(workspace_dir)
    if source_dir is None:
        source_dir = workspace_dir / "src"
    source_dir = Path(source_dir)

    if target_dir is None:
        env_target = __import__("os").environ.get("INKSCAPE_EXTENSIONS_DIR", "")
        if not env_target or not env_target.strip():
            raise ValueError(
                "INKSCAPE_EXTENSIONS_DIR is not set. Add it to the .env file, for example: "
                "INKSCAPE_EXTENSIONS_DIR=C:/Users/<you>/.config/inkscape/extensions"
            )
        target_dir = Path(env_target)

    target_dir = Path(target_dir).expanduser()
    if not str(target_dir) or not str(target_dir).strip():
        raise ValueError(
            "INKSCAPE_EXTENSIONS_DIR is not set. Add it to the .env file, for example: "
            "INKSCAPE_EXTENSIONS_DIR=C:/Users/<you>/.config/inkscape/extensions"
        )
    target_dir.mkdir(parents=True, exist_ok=True)

    for file_name in FILES_TO_INSTALL:
        source_path = source_dir / file_name
        if not source_path.exists():
            raise FileNotFoundError(f"Required extension file not found: {source_path}")
        destination = target_dir / file_name
        shutil.copy2(source_path, destination)
        if verbose:
            print(f"  copy: {source_path.name} -> {destination}")

    if verbose:
        print(f"Deployed to: {target_dir}")
    return target_dir


def package(
    workspace_dir: str | Path = ROOT,
    output_dir: str | Path = DIST_DIR,
    verbose: bool = False,
    release: bool=False
) -> tuple[Path, Path|None]:
    _clean_dist(output_dir=output_dir, verbose=verbose)
    zip_file = _bundle_files(workspace_dir=workspace_dir, output_dir=output_dir, verbose=verbose, release=release)

    signature_file = None

    if release:
        signature_file = _sign_release(zip_file, verbose=True)
    
    return (zip_file, signature_file)


def setup(
    workspace_dir: str | Path = ROOT,
    venv_dir: str | Path | None = None,
    requirements_file: str | Path | None = None,
    recreate: bool = False,
) -> Path:
    workspace_dir = Path(workspace_dir)
    if venv_dir is None:
        venv_dir = workspace_dir / ".venv"
    venv_dir = Path(venv_dir).expanduser()

    if requirements_file is None:
        requirements_file = workspace_dir / "requirements.txt"
    requirements_file = Path(requirements_file)

    if not requirements_file.exists():
        raise FileNotFoundError(f"Requirements file not found: {requirements_file}")

    if recreate and venv_dir.exists():
        shutil.rmtree(venv_dir)

    if not venv_dir.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    if os.name == "nt":
        python_executable = venv_dir / "Scripts" / "python.exe"
    else:
        python_executable = venv_dir / "bin" / "python"

    subprocess.run([str(python_executable), "-m", "pip", "install", "-r", str(requirements_file)], check=True)
    return python_executable


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and publish the GT7 Inkscape extension.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    release_parser = subparsers.add_parser("release", help="Create a signed zip bundle for release.")
    release_parser.add_argument("--output-dir", default=str(DIST_DIR), help="Where to write the release zip.")
    release_parser.add_argument("--verbose", action="store_true", help="Print each file added to the release bundle.")

    release_parser = subparsers.add_parser("package", help="Create an unsigned zip bundle for local deployment.")
    release_parser.add_argument("--output-dir", default=str(DIST_DIR), help="Where to write the release zip.")
    release_parser.add_argument("--verbose", action="store_true", help="Print each file added to the release bundle.")

    deploy_parser = subparsers.add_parser("deploy", help="Copy the extension files into the local Inkscape extensions directory.")
    deploy_parser.add_argument("--target-dir", default=None, help="Optional override for the Inkscape extensions directory.")
    deploy_parser.add_argument("--source-dir", default=str(SRC_DIR), help="Source directory containing gt7_output.py and gt7_output.inx.")
    deploy_parser.add_argument("--verbose", action="store_true", help="Print each file copied into the Inkscape extensions directory.")

    setup_parser = subparsers.add_parser("setup", help="Create a .venv and install Python dependencies from requirements.txt.")
    setup_parser.add_argument("--venv-dir", default=str(ROOT / ".venv"), help="Directory for the virtual environment.")
    setup_parser.add_argument("--requirements", default=str(ROOT / "requirements.txt"), help="Requirements file to install.")
    setup_parser.add_argument("--recreate", action="store_true", help="Delete and recreate the virtual environment before installing dependencies.")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "package":
        archive = package(output_dir=args.output_dir, verbose=args.verbose, release=False)
        print(f"Development bundle created: {archive}")
        return

    if args.command == "release":
        archive = package(output_dir=args.output_dir, verbose=args.verbose, release=True)
        print(f"Release bundle created: {archive}")
        return

    if args.command == "deploy":
        install_target = deploy(target_dir=args.target_dir, source_dir=args.source_dir, verbose=args.verbose)
        print(f"Deployed to: {install_target}")
        return

    if args.command == "setup":
        python_path = setup(workspace_dir=ROOT, venv_dir=args.venv_dir, requirements_file=args.requirements, recreate=args.recreate)
        print(f"Environment ready: {python_path}")
        return

    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
