# Publishing checklist

1. Verify that the included Sims 4 Mod Manager-generated template still contains only the neutral placeholder and no third-party custom artwork.
2. Test the updater after the latest Sims 4 patch.
3. Test on a clean Mods folder with no competing main-menu replacement.
4. Run `Update_Main_Menu.bat` and verify the generated lot background.
5. Run `Update_And_Launch_TS4.bat` and verify launch behavior on EA App and, if possible, Steam.
6. Create a Git tag such as `v0.1.0` and push it. GitHub Actions will build the standalone Windows EXE and attach the ZIP to the release.
7. When uploading to CurseForge or ModTheSims, clearly state that the tool reads local save files but does not modify them.
8. Mention that the background updates between game launches, not live while the game is running.
9. Include the tested Sims 4 game version in every release description.
