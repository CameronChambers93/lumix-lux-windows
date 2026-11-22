#!/bin/sh
# Builds dist/lumix-pc-<version>.zip: the web app, core's camera runtime and drivers, plus lumix.bat. Usage: sh pc/release.sh <version>
set -e
cd "$(dirname "$0")/.."
v=${1:?version}
out=dist/lumix-pc-$v.zip
rm -rf dist/pc-stage "$out"; mkdir -p dist/pc-stage/lumix
cp -r app dist/pc-stage/lumix/app
rm -rf dist/pc-stage/lumix/app/__pycache__ dist/pc-stage/lumix/app/test_exif.py dist/pc-stage/lumix/app/start.sh
cp pc/lumix.bat dist/pc-stage/lumix/
cp core/docs/protocols/panasonic-camcgi.md dist/pc-stage/lumix/PROTOCOL.md
mkdir -p dist/pc-stage/lumix/core/py  # the camera runtime and drivers, at the paths server.py expects (core/py, core/drivers)
cp -r core/py/luxcamera dist/pc-stage/lumix/core/py/
cp -r core/drivers core/schema dist/pc-stage/lumix/core/
rm -rf dist/pc-stage/lumix/core/py/luxcamera/__pycache__ dist/pc-stage/lumix/app/test_diagnostics.py dist/pc-stage/lumix/app/test_server.py
(cd dist/pc-stage && python3 -c "import shutil,sys;shutil.make_archive('../lumix-pc-$v','zip','.','lumix')")
rm -rf dist/pc-stage; echo "$out"
