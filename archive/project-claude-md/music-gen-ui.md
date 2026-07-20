# Co-Compose — Codebase Overview

## What it is
A collaborative AI music generation UI. Users edit a piano roll; the AI generates notes for selected regions based on song structure. The frontend works with any backend that implements the `CoComposeServer` base class.

## Tech Stack
- **Frontend**: Vue 3 (Composition API) + TypeScript + Vite + Pinia, in `ui/`
- **Backend**: Python FastAPI, base class in `server/`, SFS model backend in `sfs_server/`
- **Audio**: Tone.js (`@tonejs/piano`) for playback; external MIDI device support via Web MIDI API
- **MIDI**: `@tonejs/midi` (forked), `miditoolkit` (Python)

## Directory Layout
```
ui/src/
  App.vue                        # Root layout, keyboard shortcuts, generation trigger
  api.ts                         # HTTP client for /api/generate/
  utils.ts                       # Note & Pianoroll classes, MIDI conversions
  player.ts                      # Audio playback (Tone.js)
  stores/store.ts                # Global state: BPS, volume, MIDI ports
  components/
    PianorollEditor.vue          # Canvas wrapper (main + velocity editor)
    pianoroll/
      PianorollEditorCore.ts     # Core rendering & input logic
      dragBehaviors.ts           # Mouse/touch drag handlers
      styles.ts                  # Chromatic layout styles
    LeftBar.vue                  # Asset library (drag MIDI templates)
    SettingsPanel.vue            # BPM, volume, MIDI out, save/load
    SectionControl.vue           # Song segment UI
    RangeSelect.vue              # Beat-range selection overlay
    ToolBox.vue                  # Context menu (Generate, Add Section, Set Seed)

server/co_compose/
  server.py                      # FastAPI app + CoComposeServer base class

sfs_server/main.py               # SFS pretrained-model backend (production)
example_server.py                # Minimal example backend (chromatic scale)
default_assets/                  # Bundled MIDI files served to the UI
```

## Starting Dev Servers

**Frontend** (proxies `/api/*` → `localhost:8000`):
```
cd ui
npm install
npm run dev        # → http://localhost:5173
```

**Backend** (example, no model):
```
pip install -e server
python example_server.py   # → http://localhost:8000
```

**Backend** (SFS model, needs checkpoint in `sfs_server/`):
```
pip install -e server
pip install git+https://github.com/eri24816/segmented-full-song-gen.git
python sfs_server/main.py
```

## API Contract

**POST `/api/generate/`** (multipart):
- `midi_file`: current MIDI blob
- `params` (JSON): `{ range: {start, end}, segments: [{start, end, label, is_seed}], song_duration }`
- `client_id`: UUID (sending same ID cancels previous request)

Response: newline-delimited JSON, each line `[onset, pitch, velocity, duration]` (beats, MIDI pitch, 0–127, beats).

**GET `/api/default_assets/`** — list MIDI files  
**GET `/api/default_assets/{name}`** — retrieve MIDI file

## Key Conventions
- **Time unit**: beats (not quarter notes). Default BPM: 108.
- **Pitch range**: 21–108 (A0–C8).
- **Snap grid**: configurable in `PianorollEditorCore` (default 0.25 = 1/4 beat).
- **Seed segment**: exactly one segment marked `is_seed`; used by the SFS model for conditioning.
- **Streaming**: notes stream in real-time for progressive UI updates.

## Implementing a Custom Backend
Subclass `CoComposeServer` and override `generate()`:
```python
from co_compose import CoComposeServer, GenerateParams

class MyServer(CoComposeServer):
    async def generate(self, midi, params: GenerateParams, cancel_event):
        # yield (onset_float, pitch_int, velocity_int, duration_float)
        yield (0.0, 60, 80, 1.0)
```

## Keep This File Updated
Update this file whenever significant structural changes are made to the repo (new components, new API routes, changed conventions, added dependencies).
