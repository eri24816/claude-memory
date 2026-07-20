cd backend
uvicorn main:app --reload

cd frontend
install: 
npm install
lint:
npm run lint
run:
npm run dev

## Repo Structure

```
midi-browser/
├── backend/
│   └── main.py          # FastAPI server — serves /api/files, /api/file, /api/root
│                        # MIDI_ROOT env var (default: D:\piano-ai\pop80k_k\synced_midi)
│                        # Runs on port 8000; Vite proxies /api → 8000
└── frontend/            # Vite + React + TypeScript
    └── src/
        ├── App.tsx              # Root layout: resizable sidebar + pianoroll area
        ├── utils.ts             # Pianoroll data model (Note, Pianoroll, parse/export MIDI)
        ├── player.ts            # @tonejs/piano wrapper (AutoKeyupPiano, AnyPiano)
        └── components/
            ├── FileExplorer.tsx         # Tree-view file browser (calls backend /api/files)
            ├── PianorollEditor.tsx      # React wrapper around PianorollEditorCore canvas
            ├── PianorollSettings.tsx    # Settings panel UI (theme/noteShape/noteColor/bpm)
            └── pianoroll/
                ├── PianorollEditorCore.ts  # Canvas engine: render, playback, drag, zoom/pan
                │                           # Exports PianorollSettings interface + setSettings()
                ├── styles.ts               # Visual style system:
                │                           #   Theme (bg/grid colors), NoteShape (sustain/onsetSustain),
                │                           #   NoteColorScheme (chromatic/fruity, mode: normal|dimmed)
                │                           #   composeStyle(theme, noteShape, noteColor) factory
                └── dragBehaviors.ts        # Pointer drag handlers (pan, note edit, etc.)
```

### Key wiring
- `App.tsx` holds `PianorollSettings` state → passes to both `<PianorollSettingsPanel>` and `<PianorollEditor>`
- `PianorollEditor` calls `coreRef.current.setSettings(s)` on settings change
- Space/Tab key toggles playback in `PianorollEditorCore`
- Pitch range auto-fits MIDI content (min span 15 semitones)