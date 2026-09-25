import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def load_env(env_file):
    env_file = Path(env_file)
    print(f"Loading env from: {env_file.resolve()}")

    if not env_file.exists():
        raise FileNotFoundError(env_file)

    for line in env_file.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ[key] = value
        print(f"Loaded {key}={value}")


load_env("../.env")

test_profile = Path(tempfile.gettempdir()) / "InkscapeDeployTest"

shutil.rmtree(test_profile, ignore_errors=True)
test_profile.mkdir(parents=True)

env = os.environ.copy()
env["INKSCAPE_PROFILE_DIR"] = str(test_profile)
print(f"INKSCAPE_PROFILE_DIR={env["INKSCAPE_PROFILE_DIR"]}")

inkscape = Path(env["INKSCAPE_EXE"])

if not inkscape.exists():
    raise FileNotFoundError(inkscape)

try:
    print("Launching ", inkscape)
    
    proc = subprocess.Popen(
        [str(inkscape)],
        env=env,
    )
    
    try:
        ret = proc.wait(timeout=5)
        print(f"Exited immediately with code {ret}")

    except subprocess.TimeoutExpired:
        print(f"Launched Inkscape with PID {proc.pid}")
        print("Inkscape is still running after 5 seconds.")

except Exception as ex:
    print("Launch failed:")
    print(repr(ex))
    raise