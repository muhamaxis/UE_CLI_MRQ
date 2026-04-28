# MRQ Launcher (CLI)

**MRQ Launcher (CLI)** is a lightweight desktop launcher for running **Unreal Engine Movie Render Queue (MRQ)** jobs without opening the full Unreal Editor.

The launcher is designed to be **Unreal Engine version agnostic**: it does not depend on one fixed UE installation. You point it to the `UnrealEditor-Cmd.exe` from the engine version you want to use, and the launcher builds command-line MRQ jobs for that version.

> Batch render Unreal Movie Render Queue jobs from a clean desktop UI.

---

## Screenshots

### Qt Shell

![MRQ Launcher Qt Shell](docs/images/mrq_launcher_qt_full.png)

### Minimal Mode

![MRQ Launcher Minimal Mode](docs/images/mrq_launcher_qt_minimal.png)

---

## What it does

- Creates and manages render jobs based on `.uproject`, Map/Level, Level Sequence, and MRQ Preset.
- Works with different Unreal Engine installations by selecting the required `UnrealEditor-Cmd.exe`.
- Enables, disables, duplicates, reorders, removes, and filters jobs.
- Tracks explicit render order for enabled jobs.
- Runs enabled or selected jobs as a controlled render queue.
- Appends new jobs to the active queue while rendering is already in progress.
- Prevents accidental parallel Unreal render launches from the launcher UI.
- Supports **Stop Current Render** without cancelling the remaining queue.
- Supports **Stop All** for full queue cancellation.
- Saves and loads full render queues as JSON.
- Saves and loads individual task files as `.task.json`.
- Shows command preview and live render log.
- Writes per-task logs and queue summary logs into `mrq_logs/`.
- Provides a compact **Minimal Mode** focused only on active render jobs.

---

## Why use it

MRQ Launcher is useful when you need repeatable Unreal render automation without keeping the full editor open.

It is especially practical for:

- cinematic rendering
- batch MRQ jobs
- overnight render queues
- technical artists and lighting artists
- small teams that need a simple render launcher
- projects that move between multiple Unreal Engine versions

---

## Unreal Engine compatibility

The launcher is intended to be universal across Unreal Engine versions that support command-line MRQ rendering through `UnrealEditor-Cmd.exe`.

Tested versions may vary by project, but the workflow is not hardcoded to a specific engine version. To use another engine version, select its executable, for example:

```text
C:/Program Files/Epic Games/UE_5.4/Engine/Binaries/Win64/UnrealEditor-Cmd.exe
C:/Program Files/Epic Games/UE_5.5/Engine/Binaries/Win64/UnrealEditor-Cmd.exe
C:/Program Files/Epic Games/UE_5.6/Engine/Binaries/Win64/UnrealEditor-Cmd.exe
C:/Program Files/Epic Games/UE_5.7/Engine/Binaries/Win64/UnrealEditor-Cmd.exe
```

The actual render result still depends on your Unreal project, MRQ preset, plugins, engine build, and command-line support in the selected UE version.

---

## Requirements

- Windows
- Unreal Engine with `UnrealEditor-Cmd.exe`
- Python 3.9+
- Optional: PySide6 for the Qt shell

---

## How to run

1. Download or clone this repository.
2. Open the project folder.
3. Run the launcher using the provided batch file.

For the classic launcher:

```bat
mrq_launcher.bat
```

For the Qt shell, use the Qt run/build script provided by the project if available.

---

## Quick start

1. Set the path to `UnrealEditor-Cmd.exe`.
2. Add a job:
   - select your `.uproject`
   - select the Map/Level asset
   - select the Level Sequence asset
   - select the MRQ Preset asset
3. Enable the jobs you want to render.
4. Check the render order in the **Order** column.
5. Click **Render Enabled** or **Render Selected**.
6. Use **Minimal Mode** during rendering if you want a compact execution view.
7. Open logs from the launcher when the render finishes.

---

## Queue behavior

The launcher uses one controlled runtime queue.

- **Render Enabled** starts or appends enabled jobs.
- **Render Selected** starts or appends selected jobs.
- **Append Selected to Render Queue** adds jobs to the current queue.
- If a render is already running, new jobs are appended instead of starting a second Unreal process.
- Disabled jobs are hidden in Minimal Mode.
- Queue order is shown explicitly in the **Order** column.

---

## Saving and loading

- **Save Queue** stores the full queue and render settings into one JSON file.
- **Load Queue** restores the full queue later.
- **Save Selected Task(s)** exports selected jobs as `.task.json`.
- **Load Task(s)** imports one or more saved jobs.

---

## Logs

Logs are written into:

```text
mrq_logs/
```

The launcher can open the logs folder and the latest log for the selected job directly from the UI.

---

## Repository screenshots

Screenshots are stored in:

```text
docs/images/
```

Recommended file names:

```text
docs/images/mrq_launcher_qt_full.png
docs/images/mrq_launcher_qt_minimal.png
```

Markdown references use relative paths, so the images will display correctly on GitHub after they are committed.

---

## License

MIT. Free to use, modify, and share.
