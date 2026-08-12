# HD2 AutoPatcher

Desktop patch migration tool for Helldivers 2 mods.

It updates supported Unit, Particle, and Wwise audio patch data for the current game format. It also includes a safe automatic mode for verified rigged weapon ID swaps and the legacy manual source-archive workflow for armor/helmet ID swaps.

## Run from source

1. Install Python 3.10 or newer.
2. Open `hd2_patch_fixer` and run `run.bat`.
3. On first use, install the dependencies when prompted by the script:

   ```bat
   python -m pip install -r requirements.txt
   ```

Select the Helldivers 2 `data` folder, choose a patch or compressed mod, then run the patcher.

## Build an EXE

Run `hd2_patch_fixer\build.bat`. The output is `hd2_patch_fixer\dist\HD2PatchFixer.exe`.

The bundled community audio source is required for Wwise Bank migration. No Wwise desktop installation is required for normal patch migration.

## Compressed mods

Small compressed mods can use up to four parallel patch jobs. Very large manifests automatically use two jobs to keep RAM usage under control.

Thanks to Eve, Box, and everyone contributing to the Helldivers 2 modding community.
