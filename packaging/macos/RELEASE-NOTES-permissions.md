# macOS folder permissions (release note text)

Fast File Search Pro indexes file names in folders you choose. File contents are not read.

**First launch.** macOS shows one permission prompt for each protected folder the app scans:
Desktop, Documents, Downloads, and removable or network volumes. Choose Allow to let the
app index that folder. If you choose Don't Allow, the app shows a warning for that folder and does
not ask again. To change your answer, open System Settings, Privacy & Security, Files and Folders
(or Full Disk Access) and switch Fast File Search Pro on.

**One permission for everything.** Grant Full Disk Access in System Settings, Privacy & Security,
Full Disk Access. The app then does not need the per-folder permissions.

**After an update.** The app is signed ad hoc (no Apple Developer ID), so macOS may treat each new
build as a different app and forget earlier permissions. If a folder shows as not allowed after
updating, allow it again in System Settings. This will stay the case until the app is signed with a
Developer ID.

**First open.** The app is not notarized. Right-click the app, choose Open, then Open (macOS 15
and later: System Settings, Privacy & Security, Open Anyway).

The usage text macOS shows with each prompt is: "Fast File Search Pro indexes file names in folders
you choose so you can find them instantly. File contents are not read."
