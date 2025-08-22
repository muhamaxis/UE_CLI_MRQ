# MRQ Launcher (CLI)

**MRQ Launcher (CLI)** is a lightweight desktop tool for running **Unreal Engine Movie Render Queue (MRQ)** tasks without opening the full Unreal Editor.  
It provides a simple graphical interface (based on Tkinter) where you can:

- Create and manage a list of render tasks (`.uproject`, Map/Level, Level Sequence, MRQ Preset).
- Enable/disable, duplicate, reorder or remove tasks.
- Run **all**, **selected**, or **enabled** tasks in batch.
- Add new tasks to the queue even while rendering is in progress.
- Save and load tasks or whole queues as JSON files for reuse.
- Track render status, elapsed time, and results in a clear table.
- Review logs for each task (logs are saved in `mrq_logs/`).

The tool is especially useful for artists, technical directors, and teams who need to batch render cinematic sequences from Unreal without dealing with command-line complexity.

---

## Requirements

- **Windows**  
- **Unreal Engine 5.4 / 5.5 / 5.6** (tested)  
- Python **3.9+** (already included with most UE installations or can be installed from [python.org](https://www.python.org/))  

---

## How to Run

1. Download or clone this repository.  
2. Open the project folder.  
3. Double-click **`mrq_launcher.bat`**.  

   This will automatically start the MRQ Launcher using Python.  
   *(No need to use the command line — Unreal Engine usually comes with Python preinstalled.)*

---

## Quick Start

1. In the top field, set the path to your `UnrealEditor-Cmd.exe`.  
   (Usually in `C:/Program Files/Epic Games/UE_5.x/Engine/Binaries/Win64/`)  

2. Add a task:  
   - Select your `.uproject` file.  
   - Pick a Map (`.umap`).  
   - Pick a Level Sequence asset.  
   - Pick an MRQ Preset asset.  

3. Add as many tasks as you need. Use the right panel to enable/disable, reorder, or duplicate tasks.  

4. Choose one of the render options:  
   - **Render Selected**  
   - **Render Checked**  
   - **Add Task(s) to Queue** (even while rendering is in progress)  

5. Logs are written into `mrq_logs/` and can be opened directly via buttons at the bottom of the window.  

---

## Saving and Loading

- **Save Queue**: stores the entire list of tasks and settings into one JSON.  
- **Load Queue**: restores them later.  
- **Save Selected Task(s)**: exports only chosen tasks as `.task.json`.  
- **Load Task(s)**: imports one or more saved tasks.  

---

## Why use MRQ Launcher?

- No need to keep Unreal Editor open during long renders.  
- Simplifies batch rendering workflows.  
- Easy for artists / non-programmers — just click to set up tasks.  
- Provides clear feedback and logs for each task.  

---

## License

MIT — free to use, modify, and share.  

---

👉 Suggested tagline for the repo:  
**“Batch render Unreal Movie Render Queue without opening the Editor.”**
