# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['ps5_image_forge_linux/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ps5_image_forge_linux/resources', 'ps5_image_forge_linux/resources'),
        ('ps5_image_forge_linux/exfat_helper.sh', 'ps5_image_forge_linux'),
        ('ps5_image_forge_linux/root_helper.py', 'ps5_image_forge_linux'),
    ],
    hiddenimports=[
        # lazy_mkpfs: imported at runtime via sys.path, must be explicitly listed
        'lazy_mkpfs',
        'lazy_mkpfs.build',
        'lazy_mkpfs.compression',
        'lazy_mkpfs.create_exfat',
        'lazy_mkpfs.consts',
        'lazy_mkpfs.types',
        'lazy_mkpfs.utils',
        'lazy_mkpfs.pbar',
        'lazy_mkpfs.pack_folder',
        'lazy_mkpfs.pack_file',
        'lazy_mkpfs.crypto',
        'lazy_mkpfs.inspect',
        'lazy_mkpfs.ampr_index',
        # cryptography: imported by lazy_mkpfs but not detected by PyInstaller
        'cryptography',
        'cryptography.hazmat',
        'cryptography.hazmat.backends',
        'cryptography.hazmat.backends.openssl',
        'cryptography.hazmat.primitives',
        'cryptography.hazmat.primitives.ciphers',
        'cryptography.hazmat.primitives.ciphers.algorithms',
        'cryptography.hazmat.primitives.hashes',
        'cryptography.hazmat.primitives.padding',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ps5-image-forge-linux',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ps5-image-forge-linux',
)
