# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/fonts/AlbulaPro', 'assets/fonts/AlbulaPro')],
    hiddenimports=['x_search', 'douyin_ws', 'auto_setup', 'auto_update', 'certifi', 'websockets', 'websockets.legacy', 'websockets.legacy.server'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OBNewsVideoPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OBNewsVideoPro',
)
app = BUNDLE(
    coll,
    name='OB-NewsVideo Pro.app',
    icon='icon.icns',
    bundle_identifier='com.ob.newsvideo-pro',
    info_plist={
        'CFBundleDisplayName': 'OB-NewsVideo Pro',
        'CFBundleName': 'OB-NewsVideo Pro',
    },
)
