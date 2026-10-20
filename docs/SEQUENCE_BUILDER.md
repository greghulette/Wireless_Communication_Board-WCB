# Sequence Builder

The Wizard's timeline editor for one stored sequence. A user drags commands from the command library onto a timeline,
places each one to the millisecond, and Apply writes the result back into the sequence's row. From there SAVE/UPDATE,
TEST and a push send it exactly as they send typed text, so the builder adds no wire path of its own. Every row also
shows a timing strip: when each of its commands fires, coloured by device.

| File | What |
|---|---|
| `Wizard/seq-builder.js` | The builder: a pure core (parse, compile, analyze, routes, library decode) that also loads under Node, and the UI |
| `Wizard/seq-builder.css` | Its styles, built on the Wizard's theme tokens |
| `Wizard/cmdlib/droidnet/` | The DroidNet command library: a copy of NaviCore's vendored snapshot (see its `NOTICE.md`) |
| `Wizard/cmdlib/wcb/` | WCB boards the snapshot lacks: Maestro at the firmware's ranges, DFPlayer, variables, PWM, serial text |
| `Wizard/app.js` | Three hooks: the TIMELINE button and timing strip in `appendSequenceRow`, the strip's refresh in `updateSeqValCount` |
| `Wizard/index.html` | The stylesheet, the script (after `app.js`), and the **+ Timeline** button in the Stored Sequences header |

---

## 1. How a timeline becomes a sequence

The firmware has no notion of absolute time in a sequence. A sequence is one delimited chain. A `;T<ms>` token starts a
new group, which fires `<ms>` after the group before it fired, and the commands between two delays run together
(`parseCommandGroups`, `command_timer.cpp:98`). So each step on the timeline carries an absolute time, and `sbCompile`
turns those times into gaps:

- Steps sort by time. Steps at the same time keep their order, which the inspector can change (↑ ↓). That order is the
  order the board queues them.
- One `;T` per distinct time carries the gap from the previous time. A sequence that starts late opens with one.
- A gap over 30 minutes is written as several delays. The board caps each delay at 30 minutes
  (`parsedDelayLimit`, `command_timer.cpp:198`) and adds consecutive delays together (`:215`).
- A step's **IF** is written *after* its delay, immediately before its command. Written before the delay, a false
  condition would skip the delay as well (`ifGateConsumeToken` drops a gated pure delay, `WCB_Variables.cpp:477`), so
  every later step would run early. The timeline can't draw a delay that exists only sometimes, so it never writes one.
- A step's note rides inline (`cmd***note`). A note on the timeline itself (a marker) stands on its own (`^^***text`).
  Those are the forms the Wizard's text editor writes (`seqTextareaToValue` / `seqValueToLines`), so Apply's text gives
  the builder's value back exactly. The firmware drops every comment before running a recalled body
  (`WCB_Storage.cpp`, the strip before `:752`).

`sbParse` is the inverse, and mirrors `parseCommandGroups`: times accumulate over delay tokens (`;T` and `;t` alike,
including `;t<ms>,<cmd>`), an IF attaches to the next command, and comments follow the text editor's rules. A comment
first or after `^^` becomes a marker; after a single `^` it becomes the previous step's note. A marker remembers whether
steps at its time came before it, so it is written back on the same side of them.

The core round trip holds for anything the builder writes: `compile(parse(v)) === v`, and
`seqTextareaToValue(seqValueToLines(v)) === v`. The unit tests check both, the second against the functions in
`app.js` themselves.

### What opening a sequence can change

A sequence the builder did not write opens as it would run, and parsing records anything the timeline draws
differently. Those notes appear as a toast on opening and under the timeline:

| Read | Shown as | Written back as |
|---|---|---|
| `IF,c^;T500^cmd` | `cmd` at +500 ms, with IF `c` | `;T500^IF,c^cmd`. On the board a false `c` no longer also skips the 500 ms |
| `;T2000000` | 30:00 | `;T1800000`, which is what the board ran |
| `;T-500` | the board refuses the whole chain (`command_timer.cpp:189`); flagged as an error | nothing until it is fixed |
| An IF with nothing after it | dropped, with a note | dropped |

Apply on a sequence that was not changed leaves its row untouched, so nothing is re-saved that the user did not edit
(the Wizard's W-10 rule, `_seqRowValue`).

### Sequences it will not open

A chain carrying `?SEQ,SAVE`, `?CS` or `?MGMT,` never reaches the timer engine: the board runs it without any of its
delays (`chainCarriesValueVerb`, `WCB.ino:2732`). The builder refuses to open such a sequence and says to edit it as
text. It reports adding such a command to a timed sequence as an error.

## 2. What it warns about

`sbAnalyze` reports where the board would not run the sequence as drawn. Errors block Apply.

| Level | When | Why (firmware) |
|---|---|---|
| error | A command contains the delimiter | It would split into separate commands |
| error | A command is a delay or an IF | Time comes from the timeline, conditions from the IF field |
| error | A step runs this sequence itself | Refused by the cycle guard (docs/SEQUENCE_INVENTORY.md §3c) |
| error | A field is out of its declared range, or a required one is empty | The library's `params` |
| error | Over 3,999 characters | The store's limit (docs/SEQUENCE_INVENTORY.md §3d) |
| warn | A step runs a sequence that has delays of its own, and steps follow it | One timed chain runs at a time: starting the second replaces the first, and this sequence's remaining steps never run (`command_timer.cpp:105`) |
| warn | A step runs a sequence this board does not have | A recall inside a body runs this board's copy only (`WCB.ino:8471`) |
| warn | Over 2,400 characters | A classic ESP32 runs short of heap saving more (docs/SEQUENCE_INVENTORY.md §3b) |
| warn | A device command went to a port by default | No board has a port labelled for that device |
| info | IF in a timed sequence | Conditions are evaluated when the chain is read, at the start, not when their step fires |
| info | A gap over 30 minutes | Written as several delays |

Timing on the board drifts by about 1 ms per command in a group: `processCommandGroups` yields 1 ms after queueing each
one (`command_timer.cpp:274`), and the next gap is measured from the end of that. The builder does not compensate for
it; the preview says "give or take".

## 3. Where a command goes

Each step has a **Send to**, from its command's mode (`sbModeOf`, NaviCore's `_cmdlibDestMode` rule):

- **A WCB verb or setting** (the template starts with `;` or `?`: `;A`, `;M`, `;H`, `;L`, `;V`, `?…`). Its default is
  Auto: the command as is, which the board routes to wherever that device lives. "Run it on WCB n" wraps it as
  `;W<n>,<cmd>`. WLED commands find their WLED by id, so they stay on Auto.
- **A device's own string** (FlthyHPs, PSI Pro, Magic Panels, raw HCR `<…>`, serial text). It goes out a serial port:
  `;S<p><text>` on this board, `;W<n>,;S<p><text>` on another. "Broadcast" writes it bare, which the board sends to every
  port and the mesh, or `;W<n>,<text>` for every port on board n. A new step goes to the first port, on any loaded board,
  whose label (or configured device) matches the device (`SB_DEVICE_LABELS`, a copy of NaviCore's
  `NC_BOARD_DEVICE_LABELS`). Failing that it goes to this board's Serial 1, with a warning.

The comma form `;W<n>,` is written for every board number because it is valid for 1-20; the one-digit form is valid
only for 1-9 (`processWCBMessage`, `WCB.ino:8256`). Both forms are read. `;S<p>` strips one comma after the port digit
(`processSerialMessage`, `WCB.ino:8207`), so a body that starts with a comma is written with a second one. A route to
the board running the sequence needs no `;W`. The alias form (`;wdome,…`) is read and kept, not decoded.

A command that already exists is never rewritten until its fields or Send to change: the step keeps its text as typed.

## 4. The command library

The library is the one NaviCore's config tool presents (NaviCore `docs/CONFIG_TOOL.md` §6). The picker matches it: the
same groups (WCB pinned first, AstroPixels), collapsed sections with counts, and a search that opens every section with a
match. A command opens in a composer with its fields and a live preview of the command, plus Send to and **Add at the
playhead**. The composer's preview, or any command in the list, can also be dragged onto the timeline.

**Where it comes from.** `cmdlib/droidnet/` is a byte-for-byte copy of NaviCore's vendored snapshot, local
modifications included. Refresh it by copying NaviCore's over it, so the two tools keep presenting the same library.
`cmdlib/wcb/` is ours. Both load at page start through `sbLoadManifest` (same-origin `fetch`, so the Wizard has to be
served over http; under `file://` the panel says so). "Check online for a newer library" runs the same loader against
DroidNet's GitHub manifest, for this session only. **Import…** adds a DroidNet board file and keeps it in this browser
(`wcbWizard.cmdlibCustom`). Boards a user imported into NaviCore's tool are read too (`nc_cmdlib_custom_v1`). Both tools
are served from the same origin and share localStorage; the Wizard reads that key and never writes it.

**Normalized** (`sbNormalizeLibrary`):

| Rule | Boards / commands | Why |
|---|---|---|
| Hidden | `wcb-native` | Setup verbs, not actions (NaviCore hides it too) |
| Hidden | `maestro-native` | NaviCore firmware verbs as text (`setTarget,0,6000`); a Maestro on a WCB port doesn't understand them. `wcb-maestro`'s `;M<id>,setTarget` is the WCB form |
| Hidden | a custom board with `local` | An action on the NaviCore itself |
| Superseded | `maestro` → `wcb-maestro` | The snapshot's ranges are narrower than `WcbMaestro.cpp`'s (subroutine 0-127) |
| Hidden commands | `wcb.seqSave`, `seqList`, `seqClearAll`, `seqClear` | They manage sequences rather than run in one |
| Moved | the six sequence verbs, out of `wcb-native` when `wcb-sequences` is present | Upstream still ships them there; a fetch would list them twice |
| Not listed | a command with no `template` (rseries-logic's custom encoder) | It encodes to nothing |

Hidden boards still **decode**, so a step holding one of their commands (`?ETM,ON`) keeps its name. Decoding builds a
regex per template, as NaviCore's `_ncCommandPattern` does, and takes the most specific match. A visible board beats a
hidden one, then the most literal characters, then the fewest free-text fields. That is how `;V,x,TOGGLE` reads as
"Toggle a variable" and not as "Set x to TOGGLE". A template that is only one free-text field (serial text) never
decodes, since it would match anything. A device string no board knows becomes a serial-text step, with its text
editable. A body with a `|<n>` suffix decodes against the boards that declare a duration suffix (FlthyHPs, PSI Pro),
and the number becomes the step's "Runs for".

The library is written in the factory characters (`;`, `?`). A board configured with others has the first character of
each WCB verb translated both ways (`sbToCanon` / `sbFromCanon`). Device strings are left alone.

## 5. On the page

- **Timeline.** Each lane is the device a command reaches (`sbLaneOf`): a Maestro id, the MP3 Trigger, a WLED id, a
  serial port with its label, Sequences, Variables, and so on. Lane, step and row strip share one colour per device
  family. Steps too close to label at the current zoom stack into rows, and past three rows they shrink to markers
  (tooltip only). A step that starts a timed sequence, or carries a duration suffix, draws a hatched bar for how long it
  runs.
- **Moving steps.** Drag a step to move it, snapped to the grid (Alt for 1 ms); Shift-click adds to the selection; the
  arrow keys nudge (10 ms, Shift 100, Alt 1). The inspector takes an exact time in ms, with ±1/10/100 buttons. A
  multi-selection moves by an amount, or so its first step sits at the playhead. Insert gap and Close gap shift
  everything after the playhead.
- **Preview** runs the playhead in real time in the browser and flashes each step as it passes. **Save & Test** applies
  the timeline, then runs the row's own `updateSequence` and `playSequence` (TEST, this board only), and starts the
  preview alongside the board.
- **Undo/redo** keep one step per discrete change, and one per field edit.
- **Timing strip.** `renderRowThumb` parses the row's current value. It is re-drawn 150 ms after the text changes, and
  clicking it opens the builder.

## 6. Tests

| Test | Covers |
|---|---|
| `tests/wizard/unit/seq_builder.test.js` (`npm run unit`, CI) | compile, parse and both round trips; the firmware rules above; routes and recall keys; `sbAnalyze`; the real `cmdlib/` files: normalization, decode specificity, encode/decode inverse for every visible command |
| `tests/wizard/specs/seq_builder_fake.spec.js` (CI; HIL `wizard.seq_builder_*`) | in the real page against a fake board: opening a sequence as is, editing and dragging, the composer and drag-and-drop from the library, label routing, a new sequence, the warnings, Save & Test's wire. `SB_SHOTS=<dir>` saves a screenshot at each step |

## Revision log

Newest first. One row per change that altered what this document describes.

| Date | Commit | Change | Why |
|---|---|---|---|
| 2026-10-20 | (this commit) | Page written with the feature: timeline ↔ chain rules, warnings, routing, the vendored library and its normalization | Greg: build sequences from the command library on a timeline adjustable to the ms, and show sequences graphically |
