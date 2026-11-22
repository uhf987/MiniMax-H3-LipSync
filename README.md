# MiniMax H3 Batch Studio

A small desktop app that batch-renders **singing, dancing and instrument-playing videos** from still photos using a [ComfyUI](https://github.com/comfyanonymous/ComfyUI) **MiniMax H3 reference-to-video** workflow.

Instead of editing the workflow by hand for every clip (changing the audio, typing the lyrics into the prompt, setting the duration), you load a workflow once, add your photos and audio files, fill in a few fields per clip, and press **Start**. The app talks to a running ComfyUI instance through its HTTP API and renders the clips one after another.

> Not affiliated with MiniMax or ComfyUI. The app only automates a workflow you already have.
> The interface is in **English by default** and can be switched to **Turkish** with the language selector in the top-right corner. Your choice is remembered.

---

## Features

- **English / Turkish UI** – switch the language at any time; status and log messages follow the selected language.
- **Batch queue** – add many audio files, they are rendered in order (1, 2, 3, ...).
- **Automatic duration** – the length of each audio file is read automatically and written into the workflow's duration node. An optional "extra seconds" padding is available.
- **Three modes, selectable per audio row**
  - 🎤 **Sing** – lip-sync to the vocals; you type the lyrics for each clip.
  - 🎸 **Play an instrument** – you enter the instrument and an optional genre/tempo.
  - 💃 **Dance** – optional dance style/tempo.
- **Editable prompt templates** – one template per mode, with placeholders such as `{LYRICS}`, `{INSTRUMENT}`, `{GENRE_TEMPO}` and `{DANCE}`. The template replaces the prompt stored in the workflow file, so you never have to re-save the workflow.
- **Multiple photos, used in rotation** – add as many photos as you like; clips use them in order and wrap around when they run out.
- **Automatic resolution** – width/height are computed from each photo's aspect ratio (about 0.4 MP by default, multiples of 32) to avoid the square-crop / face zoom-in problem.
- **Fast-mode toggle** – switch the Lightning LoRA on or off from the UI.
- **Workflow settings editor** – every plain value in the workflow (step counts, sampler, scheduler, fps, ...) can be changed from the UI. Changes are remembered and applied at submit time; your workflow file is never modified.
- **Results in the app** – each finished clip is shown next to its row and saved to disk together with the exact prompt that was sent.
- **Stop button** – interrupts the current render and skips the rest of the queue.

---

## Requirements

- **Python 3.10+**
- A running **ComfyUI** instance with the MiniMax H3 nodes and models installed (default address `http://127.0.0.1:8188`)
- A MiniMax H3 reference-to-video workflow exported in **API format** (see below)
- Python packages: `flask`, `requests`, and (optionally) `pywebview`
- On Windows, the desktop window uses the Microsoft **WebView2 Runtime**, which is preinstalled on current Windows 10/11 systems

---

## Installation

```bash
git clone <your-repo-url>
cd <your-repo-folder>
pip install flask requests pywebview
```

`pywebview` is optional. Without it the app falls back to opening the interface in your default browser.

## Usage

1. Start ComfyUI.
2. Run the app:
   ```bash
   python sarki_arayuz.py
   ```
3. In the app:
   1. Select your **workflow** (API-format `.json`).
   2. Add one or more **photos**.
   3. Add your **audio files**. For each row choose a **mode** and fill in the fields (lyrics / instrument / dance style).
   4. Optionally review the **prompt template** for each mode and the **workflow settings**.
   5. Press **Start**.

Tip: try one short clip first to make sure the node IDs, duration and prompt look right before running a long queue.

### Output

Every run creates a timestamped folder next to the app (or next to the `.exe`):

```
outputs/
  20261122_153012/
    01_song.mp4
    01_prompt.txt      <- the exact prompt that was sent to ComfyUI
    02_dance.mp4
    02_prompt.txt
    ...
```

Settings (ComfyUI address, node IDs, templates, workflow edits) are stored in a `webview_data` folder next to the app when running in the desktop window.

---

## Workflow requirements

The app expects a workflow saved in **API format** (in ComfyUI: *Save (API Format)* / *Export (API)*), not the regular UI-format JSON. The default node IDs match the MiniMax H3 reference-to-video workflow this app was built around:

| Purpose | Default node ID | Node type |
|---|---|---|
| Reference photo | `137` | `LoadImage` |
| Audio | `200` | `LoadAudio` |
| Prompt text | `138` | `PrimitiveStringMultiline` |
| Duration (seconds) | `132` | `PrimitiveFloat` |
| Seed | `129` | `RandomNoise` |
| Video output | `92` | `SaveVideo` |
| Generation node (width / height) | `136` | `MiniMaxH3ReferenceToVideo` |
| Lightning LoRA switch | `146` | `PrimitiveBoolean` |

If your workflow uses different IDs, change them under **Advanced: node IDs** in the app. All three modes use the **same workflow**; only the prompt differs.

For each clip the app:

1. uploads the photo and audio to ComfyUI,
2. fills the prompt template and writes it into the prompt node,
3. sets the duration node to the audio length,
4. sets a fresh random seed,
5. optionally sets width/height from the photo's aspect ratio and the Lightning switch,
6. applies your workflow-setting overrides,
7. queues the prompt, waits for the result, and downloads the video.

---

## Building a Windows executable

```bash
pip install flask requests pywebview pyinstaller
pyinstaller --noconfirm --onefile --windowed --name LipSyncRender sarki_arayuz.py
```

On Windows you can also just run `exe_yap.bat`. The executable is created in `dist/`. The `outputs` and `webview_data` folders are created next to the `.exe`.

If an antivirus flags the `--onefile` build, use `--onedir` instead.

---

## Tips for better results

- **Use a photo where the whole subject is visible.** For dancing and instrument playing the model has to invent whatever is not in the frame.
- **Keep clips short** (roughly 5–10 s). Longer audio tends to drift.
- **Use clean audio.** For singing, an isolated vocal track usually lip-syncs better than a full mix.
- **Turn off fast mode for quality.** The 4-step Lightning LoRA is quick but can reduce detail and motion quality.
- **Raise the megapixel value** (Advanced) if the face or hands are too small in the frame, at the cost of VRAM and render time.
- The lyrics you type are only a hint for the model; mouth movement is driven mostly by the audio itself.

---

## Troubleshooting

| Problem | What to check |
|---|---|
| Nothing happens when pressing Start | A red message appears next to the buttons (missing workflow, photo, audio or required field). |
| `Node 'NNN' (...) not found in the workflow` | A node ID does not exist in your workflow. Fix it under *Advanced: node IDs*. |
| `/prompt rejected` | ComfyUI rejected the workflow. Read the message in the log; usually a missing model or an invalid value. |
| `Template has unfilled field(s)` | The template contains a placeholder that no field provides. Edit the template. |
| Window does not open on Windows | Install the Microsoft WebView2 Runtime, or uninstall `pywebview` to use the browser fallback. |
| Output is square / zoomed on the face | Make sure **automatic resolution** is enabled. |

---

## Notes and limitations

- Clips are rendered **one at a time**, in queue order. There is no built-in video concatenation.
- Progress is shown as elapsed time per clip, not as a percentage.
- Uploaded photos and audio are copied into ComfyUI's `input` folder with a `ui_` prefix and are not cleaned up automatically.
- The local server listens on `127.0.0.1` only (port `7860`, or a free port if it is taken).

---

## License

Add the license of your choice here (for example MIT).
