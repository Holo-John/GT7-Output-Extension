default:
    @python build.py --help

list:
    @python build.py --help

setup:
    python build.py setup

release:
    python build.py release --verbose

deploy:
    python build.py deploy --verbose
