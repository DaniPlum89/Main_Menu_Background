# Dynamic Lot Main Menu for The Sims 4

Dynamic Lot Main Menu updates The Sims 4 main-menu background before launch so it shows the **home/current lot of the last-played household** from the newest save.

## What it does

The updater:

1. Finds the user's newest `Slot_*.save`.
2. Reads the active/last-played household from the save.
3. Finds that household's current/home zone.
4. Locates the matching `SaveGameLotThumbnail1` image stored inside the save.
5. Builds a 1280×720 background using the real lot thumbnail, with a blurred widescreen fill behind the square image.
6. Converts the texture to The Sims 4's DST1 layout.
7. Writes `ZZZ_Dynamic_Lot_Main_Menu.package` into the user's Mods folder.
8. Deletes `localthumbcache.package` so the updated texture loads on the next launch.

The updater **does not modify save files**.

## Requirements

- Windows 10 or Windows 11
- The Sims 4
- A normal Sims 4 user folder containing `saves` and `Mods`
- No Python installation is required when using the Windows release build.

## Installation

1. Download the latest Windows release ZIP.
2. Extract the ZIP somewhere outside the Sims 4 Mods folder.
3. Remove or disable any other mod that replaces the same Sims 4 main-menu background.
4. Double-click `Update_Main_Menu.bat` to update the background, or `Update_And_Launch_TS4.bat` to update it and then try to launch The Sims 4.

The updater creates:

`Documents\Electronic Arts\The Sims 4\Mods\ZZZ_Dynamic_Lot_Main_Menu.package`

## Normal use

After playing a household, **save and exit The Sims 4**. Run the updater again before your next launch. The next main menu will use the newly last-played household's saved lot thumbnail.

### Important limitation

This is a **pre-launch updater**. It cannot replace the already-loaded main-menu texture while The Sims 4 is still running. If you switch households and return to the main menu in the same session, the image will not change until you save, exit, run the updater, and launch again.

## Troubleshooting

If the updater cannot find the Sims 4 user folder, run the executable from Command Prompt with:

```text
DynamicLotMainMenu.exe --ts4-root "C:\Users\YourName\Documents\Electronic Arts\The Sims 4"
```

You can test a specific save with:

```text
DynamicLotMainMenu.exe --save "C:\path\to\Slot_00000001.save"
```

If the old menu still appears, make sure conflicting main-menu packages are removed and `localthumbcache.package` was deleted.

## Building from source

The project uses Python and Pillow. The public Windows executable is built with PyInstaller through GitHub Actions.

```text
pip install pillow pyinstaller
pyinstaller --onefile --name DynamicLotMainMenu --add-data "assets/Main_Menu_Dynamic_Template.package;." src/TS4_Lot_Menu_Updater.py
```

On Windows, PyInstaller uses `;` as the `--add-data` separator. GitHub Actions in this repository handles this automatically.

## Privacy and safety

The updater works entirely on the user's computer. It reads the newest local Sims 4 save to locate the correct lot thumbnail. It does not upload the save or thumbnail anywhere and does not write changes to the save.

## Compatibility

This project was developed and tested against The Sims 4 `1.127.41.1030`. Game updates can change save or UI resource structures, so compatibility with later versions should be retested after major patches.

## Credits / redistribution note

The updater code in `src/` is released under the MIT License. The Sims 4, its formats, trademarks, and game assets are property of Electronic Arts/Maxis.

Before publishing a release that includes `Main_Menu_Dynamic_Template.package`, make sure you have permission to redistribute every non-EA custom asset contained in that template. If the template was based on another creator's main-menu mod, replace or remove their artwork/code as required by their license or obtain permission first.
