# game26 — Design Reference

Persistent multiplayer web game inspired by Screeps. Players automate units by writing Go code executed server-side. The game runs continuously; world state persists when players log off.

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Game engine + HTTP + WS | Go | Goroutines for concurrent tick execution; fast WebSocket at scale |
| Player scripting | Go via Yaegi interpreter | Same language as engine; Yaegi allows runtime Go execution with package whitelisting |
| Frontend UI (menus, sidebar, editor) | React + TypeScript | Easy component model for HUD, selection panel, script editor |
| Game rendering | HTML5 Canvas (inside React) | DOM is too slow for 256×256 tile rendering at 60fps; Canvas is a direct pixel buffer |
| Frontend bundler | Vite | Fast HMR during development |
| Map generation | go-perlin (Perlin noise) | Produces natural-looking terrain clusters |

**Not used:**
- Ebitengine/WASM — considered but rejected; harder to build standard web UI elements
- Wire buildings — removed; replaced with simpler direct unit-carry power system

---

## World

- **Map size:** 256 × 256 tiles, single shared world per server instance
- **Tick rate:** 2 seconds per tick
- **Terrain generation:** Absolute tile coordinates (not 0–1 normalized) fed to Perlin noise, so feature size is consistent regardless of map size

### Tile Types

| Const | Description |
|---|---|
| `Empty` | Buildable, walkable |
| `Mountain` | Impassable, unbuildable |
| `IronSource` | Iron vein terrain; iron ore nodes spawn here |
| `Radiation` | Radiation terrain; radiation ore nodes spawn here; solar panels must be placed adjacent |

**Target distribution:** ~20% Mountain, ~9% IronSource, ~5% Radiation, ~64% Empty

**Noise parameters (world.go):**
- Mountain: `perlin(4 octaves) > 0.20`, scale `0.07`
- Iron: `perlin(3 octaves) > 0.30`, scale `0.12`
- Radiation: `perlin(3 octaves) > 0.36`, scale `0.09`

---

## Resource System

### Ore Nodes (Resource entities)

Ore nodes are **game objects separate from tiles**, stored in `GameState.Resources map[string]*Resource`.

| Field | Description |
|---|---|
| `Type` | `IronOre` or `RadiationOre` |
| `Pos` | Tile position |
| `Remaining` | Current capacity (decreases as mined) |
| `MaxAmount` | Initial capacity (200–500) |

**Spawning rules (per tick):**
- Each IronSource tile has a 2% chance to spawn an IronOre if no ore exists within radius 3
- Each Radiation tile has a 2% chance to spawn a RadiationOre under the same condition
- Ore disappears when `Remaining` reaches 0

### Iron Flow
```
IronSource tile → IronOre spawns → unit Harvests (Work parts) → unit Carries → unit Deposits to Storage building
```

### Energy Flow
```
Radiation tile → RadiationOre spawns → SolarPanel auto-collects adjacent ores each tick
→ unit picks up energy from SolarPanel → unit Carries → unit Deposits to Battery building
```

**No wire/grid system.** Solar panels store collected energy; units physically carry energy to batteries.

**SolarPanel placement constraint:** must be placed on a tile adjacent to (or on) a Radiation tile.

---

## Buildings

| Const | Description | HP | Storage |
|---|---|---|---|
| `Nexus` | Home base; player eliminated if destroyed | 1000 | — |
| `Factory` | Produces units (costs iron + energy) | — | — |
| `Wall` | Defensive barrier, blocks movement | — | — |
| `Road` | Movement speed ×2 | — | — |
| `Storage` | Stores iron | — | iron |
| `SolarPanel` | Auto-collects adjacent RadiationOre; units pick up energy from here | — | energy (max 500) |
| `Battery` | Stores energy carried by units | — | energy |

---

## Units

### Body Parts

| Const | Effect |
|---|---|
| `Move` | +1 tile/tick movement speed (base 0 without any Move parts) |
| `Work` | +5 iron harvest/tick from adjacent IronOre; enables building |
| `Carry` | +50 max carry capacity (shared between iron and energy) |
| `Attack` | +30 melee damage, range 1 tile |
| `RangedAttack` | +15 damage, range 3 tiles |
| `Tough` | +100 max HP (base HP is 100) |
| `Heal` | +12 HP/tick to self or adjacent friendly unit |

### Carry System
- `CarryIron` + `CarryEnergy` share the same `MaxCarry` pool
- `FreeCarry() = MaxCarry - CarryIron - CarryEnergy`

### Starter Units
Each new player spawns with 3 units: body `[Move, Work, Carry]`

---

## Player Scripting

- Players write Go code executed via **Yaegi** interpreter each tick
- Timeout: 50ms per player per tick
- Allowed packages: `game26/script/api` only (all stdlib blocked)
- Scripts queue actions; actions resolve after all scripts run

**Planned script API:**
```go
func Script(g *game.Game) {
    units := g.MyUnits()
    ores  := g.NearbyOres()
    for _, u := range units {
        u.MoveTo(x, y)
        u.Harvest(oreID)
        u.Deposit(buildingID)
        u.PickupEnergy(solarID)
        u.Attack(targetID)
    }
    g.Build(game.Storage, x, y)
    g.SpawnUnit([]string{"Move","Work","Carry"})
}
```

---

## File Structure

```
game26/
├── CLAUDE.md               ← this file
├── main.go                 ← starts HTTP server + tick engine
├── game/
│   ├── types.go            ← all enums + structs (TileType, BuildingType, BodyPart, Resource, Unit, etc.)
│   ├── world.go            ← GameState init, procedural map generation, player spawn
│   ├── tick.go             ← tick engine: runs every 2s, calls sub-systems, broadcasts state
│   ├── resources.go        ← ore spawning, solar collection, unit harvest/deposit helpers
│   ├── unit.go             ← (TODO) unit movement and action resolution
│   └── combat.go           ← (TODO) attack resolution, death, player elimination
├── script/
│   ├── executor.go         ← (TODO) Yaegi setup, package whitelist, per-player script run
│   └── api.go              ← (TODO) game.Game struct exposed to player scripts
├── server/
│   ├── http.go             ← REST: /api/state, /api/spawn; serves client/dist/
│   └── ws.go               ← WebSocket hub, broadcasts full GameState JSON each tick
├── client/                 ← React + TypeScript frontend (Vite)
│   └── src/
│       ├── types.ts        ← TypeScript mirrors of Go structs
│       ├── ws.ts           ← WebSocket client, auto-reconnect
│       ├── App.tsx         ← layout: GameCanvas + Sidebar
│       ├── GameCanvas.tsx  ← Canvas render loop; WASD+QE camera; on-screen D-pad
│       ├── renderer/
│       │   ├── map.ts      ← tile + building + ore rendering
│       │   └── units.ts    ← unit circles + HP bars
│       └── components/
│           └── Sidebar.tsx ← unit info, tick counter, resource panel
└── cmd/
    ├── maptest/            ← dev tool: generates + prints ASCII map to tune terrain params
    └── checkmap/           ← dev tool: queries live server and prints tile distribution %
```

---

## Build & Run

```bash
# build and start server (serves client/dist/ on :8080)
go build -o game26.exe . && ./game26.exe

# rebuild frontend
cd client && npm run build

# check live tile distribution
go run cmd/checkmap/main.go

# tune terrain generation (prints ASCII map + stats)
go run cmd/maptest/main.go
```

**Kill old server before restart (PowerShell):**
```powershell
Get-NetTCPConnection -LocalPort 8080 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

---

## Workflow

After every change — backend or frontend — verify the result in Chrome using the browser automation tools:
1. Rebuild (`go build` / `npm run build`) and restart the server
2. Open `localhost:8080` in Chrome via the browser tools
3. Check console errors, query `/api/state` for game state, and confirm unit behavior matches expectations
4. For movement/logic bugs: snapshot unit positions twice (~4s apart) and compare to confirm units are actually moving

---

## Conventions

- Enum constants use short names without type prefix: `Empty` not `TileEmpty`, `Nexus` not `BuildingNexus`
- `BodyPart` is `int` (not `uint8`) so `[]BodyPart` serializes as a JSON number array, not base64
- Ore node IDs are `"ore-X-Y"` (deterministic based on position)
- Building IDs: `"b-{playerID}-{type}"` for player buildings
- Unit IDs: `"u-{playerID}-{index}"`
- Camera uses absolute pixel coordinates; zoom is applied as `tileSize = BASE_TILE_SIZE * zoom`
- Zoom anchor is canvas center: `newCam = cam * ratio + (canvasSize/2) * (ratio - 1)`
- Frontend uses `const enum` disabled (`erasableSyntaxOnly: false` in tsconfig)
