# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['media_organizer.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PIL', 'workflow_editor'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy(含 numpy.libs 约 26MB) 与 yaml 是 hook 拉进来的无用负载: 源码里没有任何
    # import, PIL 导入时也不会加载 numpy(实测)。排除后包体积显著下降。
    excludes=['numpy', 'yaml'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MediaOrganizer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 关掉 UPX: 本机未装 upx 时它本来就是空操作, 而一旦在装了 upx 的机器上打包,
    # 压缩后的 Qt DLL 很容易被杀软误杀(静默终止, 与本项目历史症状混淆)。
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['media_organizer.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MediaOrganizer',
)
