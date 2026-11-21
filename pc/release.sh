#!/bin/sh
# Builds dist/lumix-pc-<version>.zip: the web app plus lumix.bat. Usage: sh pc/release.sh <version>
set -e
cd "$(dirname "$0")/.."
v=${1:?version}
out=dist/lumix-pc-$v.zip
rm -rf dist/pc-stage "$out"; mkdir -p dist/pc-stage/lumix
cp -r app dist/pc-stage/lumix/app
rm -rf dist/pc-stage/lumix/app/__pycache__ dist/pc-stage/lumix/app/test_exif.py dist/pc-stage/lumix/app/start.sh
cp pc/lumix.bat dist/pc-stage/lumix/
cp core/docs/protocols/panasonic-camcgi.md dist/pc-stage/lumix/PROTOCOL.md
(cd dist/pc-stage && python3 -c "import shutil,sys;shutil.make_archive('../lumix-pc-$v','zip','.','lumix')")
rm -rf dist/pc-stage; echo "$out"
