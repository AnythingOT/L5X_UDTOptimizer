# L5X UDT Optimizer

> **Archived.** This tool has been integrated into **[LLM4L5X](https://github.com/AnythingOT/LLM4L5X)**: attach a UDT export or a whole program there and press **optimize UDTs**. It gives the same member orders and sizes as this tool on the sample files, keeps every member's XML byte for byte, flags COP/CPS, MSG and produced/consumed risks, and can also create new UDTs. The code lives in its `udt/` folder. New work happens there; this repo is kept read-only for reference.

A local web app that reduces the memory footprint of Allen-Bradley (Studio 5000 Logix Designer) user-defined data types. It reorders UDT members by size and packs BOOLs together so the controller wastes fewer padding bytes. Every change is shown side by side before you download anything.

<img width="1892" height="1021" alt="image" src="https://github.com/user-attachments/assets/ea66334f-e145-4262-b04c-1d11f2fa92a5" />

## Read this before importing

Reordering members changes their byte offsets. Anything that reads a UDT **by name** is unaffected. Anything that copies it **by byte offset** breaks silently:

- COP / CPS copies between the UDT and arrays or other structures
- MSG instructions whose local element is the UDT
- Produced / consumed tags (the partner controller holds its own copy of the definition)
- HMI, SCADA or other devices that read the structure by offset


## What it does

Upload either a single UDT export or a full controller export (`.L5X`).

Members are ordered by alignment so no padding is wasted between them: first the 1-byte types (BOOLs, SINT), then INT, then the 4-byte types (DINT, arrays, REAL, STRING, TIMER/COUNTER/CONTROL and AOIs, nested UDTs), and last the 8-byte types (LREAL, LINT, including their arrays). Members of the same data type stay together and are sorted by name, so the result is deterministic. Consecutive BOOLs share hidden SINT host members, eight bits per host.

Preserved exactly: member names, data types, dimensions, radix (Hex, Binary, ASCII, ...), external access, member descriptions, multilingual descriptions, the UDT's own description, and dependencies.

## Results

| Result | Meaning |
|---|---|
| Reordered | Members were reordered. Expand the row to see the before and after order. |
| Already optimal | Already in the right order; the UDT is left byte-for-byte unchanged. |
| Not processed | Left unchanged on purpose. Either a member uses a type not defined in the file (its size would be a guess), or the type is a string type such as `STR20`, whose layout Studio 5000 owns. Expand the row for the reason. |
| Error | Something unexpected failed; expand the row for the message. |

## Output files

Names stay within Studio 5000's 40-character limit, so they import without renaming.

- **Single UDT** `MyUDT.L5X` → `MyUDT_opt.L5X`, a self-contained export including its nested UDTs. Import with *Data Types > User-Defined > Import Data Type*.
- **Full program** `PlantProgram.L5X` → `PlantProgram_opt.L5X`. Only reordered UDTs are changed; every other byte of the file, including rungs and comments, is identical to the original.
- **Changed UDTs zip** → `PlantProgram_opt_changed.zip`: one importable file per reordered UDT, plus `CHANGES.txt` with sizes and layout risks. Use this to import UDTs one at a time and leave risky ones out.

## Running it

Python 3.10 or newer:

```bash
pip install -r requirements.txt
python app.py
```

It opens at http://127.0.0.1:5001 and listens only on this computer. It makes no outbound network calls, so it works on offline plant networks. Uploaded files are held in memory for 30 minutes after the last use, then discarded.

## Tests

```bash
python test_core.py
```

Each fixed bug has a regression test: no member is ever dropped; CDATA and everything outside replaced UDTs survives; radix and descriptions are kept; string types and partially unresolved UDTs are never rewritten; layout-risk detection; and the app's upload → stream → repeat-download flow.

## Known limitations

- Size estimates are heuristic. Nested UDTs containing LINT/LREAL may be under-aligned in the estimate.
- The member order is the best possible under the tool's own size model. It has not been compared with the sizes Studio 5000 reports for every type; check the size shown in Studio 5000 after import.
- Existing tag values: when Studio 5000 replaces a UDT definition, check that tag values are kept. Back up the project first.
- Function block (FBD) and SFC routines are not scanned for COP/CPS.

## Licence

MIT, see `LICENSE`.
