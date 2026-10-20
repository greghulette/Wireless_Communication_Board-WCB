This directory vendors a snapshot of the **DroidNet Command Library** data files
(`manifest.json` + `boards/*.json`), so the Wizard's sequence builder has a full command
catalog available immediately, without a live fetch to GitHub.

- Source: https://github.com/travisccook/Droidnet-Command-Library
- License: Mozilla Public License 2.0 (see `LICENSE` in this directory — DroidNet's own,
  unmodified). Only the DroidNet files themselves (this directory) stay under MPL-2.0.
- Snapshot: a byte-for-byte copy of NaviCore's vendored snapshot
  (`NaviCore/config_tool/cmdlib/droidnet/`, last changed there in `b35f3cc`, 2026-08-17),
  `libraryVersion` 4.2.0. The two tools present the same library, so they carry the same
  files: refresh this directory by copying NaviCore's over it, not from upstream directly.
  NaviCore's copy has two local modifications pending an upstream PR, and so does this
  one — see NaviCore's `cmdlib/droidnet/NOTICE.md`:
  1. The six stored-sequence verbs moved out of `wcb-native.json` into
     `boards/wcb-sequences.json`.
  2. `"source": "wcb.sequences"` on the `key` param of `wcb.runSeq`, `wcb.runSeqLong` and
     `wcb.seqClear`. In the Wizard that source offers the sequence keys of every board loaded
     in the Wizard.
- These files are read at runtime by `Wizard/seq-builder.js` (`sbLoadManifest`), the same
  code path the "Check online for a newer library" fetch uses.
- The WCB's own boards that upstream lacks (Maestro with the firmware's ranges, DFPlayer,
  variables, PWM, serial text) are not here: they are in `../wcb/`, which is ours.
