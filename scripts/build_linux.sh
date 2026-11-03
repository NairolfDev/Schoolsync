#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python -m PyInstaller --noconfirm --clean --onefile --name schulsync --paths src \
  --add-data 'config.example.toml:.' --collect-data googleapiclient --collect-all tzdata \
  --collect-all keyring --collect-all secretstorage --collect-all jeepney \
  --hidden-import keyring.backends.SecretService --collect-submodules schulsync packaging/entry.py
./dist/schulsync --version

python -m build --sdist --wheel --outdir dist/python
mkdir -p dist/linux-cli AppDir/usr/bin
cp dist/schulsync dist/linux-cli/schulsync
cp README.md LICENSE dist/linux-cli/
tar -C dist/linux-cli -czf dist/SchulSync-Linux-x86_64.tar.gz .
cp dist/schulsync AppDir/usr/bin/schulsync
cat > AppDir/AppRun <<'EOF'
#!/bin/sh
exec "$APPDIR/usr/bin/schulsync" "$@"
EOF
chmod +x AppDir/AppRun
cat > AppDir/schulsync.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=SchulSync
Exec=schulsync
Icon=schulsync
Terminal=true
Categories=Education;Utility;
EOF
cat > AppDir/schulsync.svg <<'EOF'
<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" rx="12" fill="#285a94"/><path d="M13 19h38v26H13z" fill="white"/><path d="M20 28h24M20 35h24" stroke="#285a94" stroke-width="3"/></svg>
EOF

# Pinned official artifacts; do not execute an unverified mutable continuous build.
curl --fail --location --retry 3 -o appimagetool.AppImage https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
echo 'ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0  appimagetool.AppImage' | sha256sum -c -
curl --fail --location --retry 3 -o runtime-x86_64 https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64
echo '2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d  runtime-x86_64' | sha256sum -c -
chmod +x appimagetool.AppImage
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 ./appimagetool.AppImage --runtime-file runtime-x86_64 AppDir dist/SchulSync-x86_64.AppImage
chmod +x dist/SchulSync-x86_64.AppImage
APPIMAGE_EXTRACT_AND_RUN=1 ./dist/SchulSync-x86_64.AppImage --version
(
  cd dist
  sha256sum SchulSync-x86_64.AppImage > SchulSync-x86_64.AppImage.sha256
  sha256sum SchulSync-Linux-x86_64.tar.gz > SchulSync-Linux-x86_64.tar.gz.sha256
  cd python
  sha256sum ./*.whl ./*.tar.gz > SHA256SUMS
)
