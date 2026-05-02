# MRQ-Launcher

A lightweight Qt desktop launcher for running Unreal Engine Movie Render Queue jobs without opening the full Unreal Editor UI.

MRQ-Launcher is designed for production render workflows where opening the full Unreal Editor just to start MRQ jobs is unnecessary, slow, and resource-heavy. The launcher runs MRQ tasks through `UnrealEditor-Cmd.exe`, provides queue management, validation, compact monitoring, cancellation controls, and render logs.

## Current version

```text
1.10.28
```

The clean repository starts from the current Qt-only production baseline. The recommended first Git tag/release is:

```text
v1.10.28
```

This keeps the GitHub release version aligned with the internal `APP_VERSION` shown by the application.

## Main features

- Run Unreal Engine Movie Render Queue jobs from a Qt desktop UI.
- Use any installed Unreal Engine version by selecting the required `UnrealEditor-Cmd.exe`.
- Load and save render queue JSON files.
- Load and save individual task JSON files.
- Validate `.uproject`, map, level sequence, and MRQ preset paths before rendering.
- Block invalid or incomplete tasks from entering the runtime queue.
- Run jobs through one controlled runtime queue.
- Prevent accidental parallel Unreal render processes.
- Append new valid jobs while a render is already running.
- Use Stop Current to cancel only the active render.
- Use Stop All to cancel the active render and clear the pending queue.
- Use Minimal Mode for compact render monitoring.
- Save per-task logs and queue summary logs.
- Review saved queue logs in the Queue Logs viewer.

## Requirements

- Windows 10/11
- Python 3.10 or newer
- PySide6
- PyInstaller, only for building the EXE
- Unreal Engine installed locally
- A valid path to `UnrealEditor-Cmd.exe`

Install Python dependencies:

```bat
pip install -r requirements.txt
```

Expected `requirements.txt`:

```txt
PySide6
pyinstaller
```

## Repository structure

```text
MRQ-Launcher/
├─ code/
│  ├─ mrq_launcher.py
│  └─ mrq_launcher.bat
├─ docs/
│  ├─ images/
│  │  ├─ mrq_launcher_qt_full.png
│  │  ├─ mrq_launcher_qt_log.png
│  │  └─ mrq_launcher_qt_minimal.png
│  ├─ BASELINE_BEHAVIOR.md
│  └─ mrq_launcher_project_guardrails.md
├─ resources/
│  ├─ app_icon.ico
│  └─ mrq_launcher_logo_167.png
├─ tools/
│  └─ build_exe.bat
├─ README.md
├─ requirements.txt
├─ .gitignore
└─ LICENSE
```

Generated folders are intentionally not part of the repository:

```text
.venv-build/
build/
dist/
mrq_logs/
```

## Run from source

From the repository root:

```bat
python code\mrq_launcher.py
```

The app opens the Qt launcher directly.

A convenience launcher may also exist at:

```text
code\mrq_launcher.bat
```

It should only launch `code\mrq_launcher.py`. It should not contain old Tk assumptions.

## Build EXE

The canonical build script is:

```text
tools\build_exe.bat
```

Run it from Explorer or from the command line:

```bat
tools\build_exe.bat
```

The builder resolves the repository root from its own path, so it can be launched from any current directory.

The EXE application name is:

```text
MRQLauncherCLI
```

Expected outputs:

```text
dist\MRQLauncherCLI\MRQLauncherCLI.exe
dist\MRQLauncherCLI.exe
```

Temporary build files are written to:

```text
build\pyinstaller\
build\spec\
build\build_exe_log.txt
.venv-build\
```

Required resources included in the build:

```text
resources\app_icon.ico
resources\mrq_launcher_logo_167.png
```

## Basic workflow

1. Launch MRQ-Launcher.
2. Select or verify the path to `UnrealEditor-Cmd.exe`.
3. Load a queue JSON file or add tasks manually.
4. Validate the queue.
5. Enable and order the jobs that should render.
6. Start Render Selected, Render Enabled, Render All, or Queue Selected.
7. Monitor execution in Full Mode or Minimal Mode.
8. Use Stop Current to cancel only the active render.
9. Use Stop All to cancel the active render and clear the pending queue.
10. Review per-task logs and queue summary logs after completion.

## Queue and task compatibility

MRQ-Launcher preserves the existing queue/task JSON model.

Persistent task fields:

- `uproject`
- `level`
- `sequence`
- `preset`
- `output_dir`
- `notes`
- `added_at`
- `enabled`

Required render fields:

- `uproject`
- `level`
- `sequence`
- `preset`

Runtime state is not written into task JSON.

Validation results are not written into task JSON.

Session order is runtime/UI state. It must not be persisted into queue JSON unless a dedicated migration is explicitly requested.

User workflow settings, such as recent queues and auto-load-last-queue, are stored separately from queue JSON.

## Validation behavior

Validation checks that required local files are available:

- `.uproject` file exists.
- Map asset resolves to a local `.umap` candidate.
- Level Sequence resolves to a local `.uasset` candidate.
- MRQ preset resolves to a local `.uasset` candidate.

Validation statuses:

- `Ready`
- `Invalid`
- `Incomplete`
- `Unknown`
- `Not checked`

Blocking statuses:

- `Invalid`
- `Incomplete`

`Unknown` warns but does not block by default.

## Render queue behavior

MRQ-Launcher is built around one active Unreal render process.

Render actions use the shared runtime queue:

- Render Selected
- Render Enabled
- Render All
- Queue Selected

When a render is already active, new valid jobs are appended to the existing runtime queue instead of launching another Unreal process.

Duplicate queued or currently rendering task objects are skipped.

Invalid or incomplete tasks are skipped and logged.

## Cancellation behavior

Stop Current:

- stops only the currently running Unreal process;
- marks the active task as cancelled;
- allows the remaining queue to continue.

Stop All:

- stops the current Unreal process;
- clears pending queue entries;
- marks pending queued tasks as cancelled;
- prevents the remaining queue from continuing.

## Minimal Mode

Minimal Mode is a compact execution view intended for render monitoring.

It shows:

- Status
- Validate
- Level
- Sequence
- Preset
- Running Time
- current task/status/session information
- Stop Current
- Stop All
- Exit Minimal Mode

Disabled tasks are hidden in Minimal Mode.

## Logs

Per-task logs are saved under:

```text
mrq_logs\
```

Queue summary logs are also saved under:

```text
mrq_logs\
```

Queue summary logs use the compact execution snapshot format:

```text
Order / Status / Level / Sequence / Preset / Running Time / Start / End
```

Queue logs are auto-saved after queue completion. Manual save remains available.

## Documentation

The clean repository keeps development contracts in `docs/`.

Important docs:

- `docs/BASELINE_BEHAVIOR.md` captures the current production behavior.
- `docs/mrq_launcher_project_guardrails.md` defines protected rules and areas that should not be casually changed.

Use these documents before implementation tasks, refactors, packaging changes, and Codex handoffs.

## Git ignore policy

The repository should not commit generated files or local runtime data.

Important ignored paths:

```gitignore
.venv-build/
build/
dist/
*.spec
mrq_logs/
*.log
user_settings.json
*.local.json
```

`tools/` is not ignored. Build scripts should be committed.

## Release flow

Recommended clean-repo first release:

```bat
git tag v1.10.28
git push origin v1.10.28
```

Future releases should follow the internal app version where practical:

```text
v1.10.29
v1.10.30
v1.11.0
v2.0.0
```

Bump `APP_VERSION` whenever a behavior-changing patch is made.

Version bump is expected for:

- runtime behavior changes;
- queue behavior changes;
- validation changes;
- UI workflow changes;
- logging format changes;
- persistence behavior changes;
- packaging/build behavior changes.

Pure documentation changes do not require an app version bump.

## Development rules

- Keep UI text in English.
- Keep code comments in English.
- Preserve queue JSON compatibility.
- Preserve task JSON compatibility.
- Do not persist runtime state into task JSON.
- Do not persist validation results into task JSON.
- Do not persist session order into queue JSON unless explicitly requested.
- Do not add dependencies unless explicitly approved.
- Do not allow parallel Unreal render processes from launcher actions.
- Do not bypass validation, duplicate protection, runtime queue, or process guard logic.
- Do not change Unreal command construction semantics without a dedicated task.
- Do not reintroduce the legacy Tk UI.

## License

See `LICENSE`.
