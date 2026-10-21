This directory vendors a snapshot of the **DroidNet Command Library** data files
(`manifest.json` + `boards/*.json`), so NaviCore's config-tool command picker has a full
command catalog available immediately, without a live fetch to GitHub. It is upstream's
data verbatim apart from the two local modifications noted below.

- Source: https://github.com/travisccook/Droidnet-Command-Library
- License: Mozilla Public License 2.0 (see `LICENSE` in this directory — DroidNet's
  own, unmodified) — the license permits combining with proprietary code; only the
  DroidNet files themselves (this directory) stay under MPL-2.0.
- Snapshot: `libraryVersion` 4.2.0 (21 boards upstream; 22 here, see the local split
  below), fetched 2026-07-22 from the `main`
  branch. 4.2.0 (NaviCore's own upstream PR) split the Maestro board into `maestro`
  (the `;M` sequence-trigger verb) + `maestro-native` (device-native Pololu servo
  protocol), and added `wcb-wled` (the `;L` WLED verb set). Its WCB boards
  (wcb-hcr / wcb-mp3 / wcb-native) supersede NaviCore's small curated seed of the same
  ids on merge. The reverse holds for the `;M` Maestro board: the vendored `maestro`'s
  range is still narrow (id 0-2 / seq 0-9) vs the firmware's 0-9 / 0-99, so NaviCore's
  `_cmdlibNormalize()` drops the vendored `maestro` in favor of the seed's accurate
  `wcb-maestro`. The inline seed keeps the file:// fallback, that accurate `wcb-maestro`,
  and `nc-maestro` (the Pololu servo command set that routes to a NaviCore configured-
  Maestro slot). `_cmdlibNormalize()` likewise drops the vendored `maestro-native`: its
  commands are identical to `nc-maestro`'s, but it addresses a raw `;W<wcb>;S<port>` WCB
  serial port, whereas `nc-maestro` targets a configured Maestro slot (1-8) that the
  firmware resolves to a local Maestro (Serial2) or a remote one (ESP-NOW broadcast,
  disambiguated by Pololu device #) — so the user never types WCB/port numbers.
- These files are read at runtime by NaviCore's command-library picker
  (`config_tool/index.html`, `_cmdlibImportFromManifest`),
  the same code path used for a live "check for updates" fetch. NaviCore's own
  command data (WCB/HCR/MP3/WLED/Maestro, curated from the real WCB firmware) is
  separate and lives inline in `index.html` as `NC_CMDLIB_SEED`.
- To refresh this snapshot, use the "Check online for the latest library" option
  in the command-library picker, or re-download `manifest.json` + every file it
  lists under `boards/` from the source repo above.
- **Local modifications (two, both pending an upstream PR):**
  1. **The stored-sequence verbs moved to their own board.** `boards/wcb-sequences.json`
     is new and holds `wcb.seqSave`, `wcb.seqList`, `wcb.seqClearAll`, `wcb.seqClear`,
     `wcb.runSeq` and `wcb.runSeqLong`; `boards/wcb-native.json` drops from 77 commands
     to 71 and loses its now-empty `Sequences` category; `manifest.json` gains the
     matching entry. A pure move — the command objects are byte-identical — so the only
     change is where a picker lists them: their own small library, instead of buried in
     a 77-command board.
  2. **A live key picker.** `"source": "wcb.sequences"` on the `key` param of
     `wcb.runSeq`, `wcb.runSeqLong` and `wcb.seqClear`, so NaviCore's picker offers the
     sequences a WCB actually holds (pulled off the mesh) instead of a free-text box.
     The param's `pattern` is untouched, so a consumer that doesn't implement the field
     ignores it and still gets a text input — which is what makes it safe to send
     upstream.

  Everything else here is byte-for-byte upstream. Because a refresh replaces a board file
  wholesale, `_cmdlibApplySeqSource()` re-asserts the field and `_cmdlibDropMovedCmds()`
  re-applies the split after every merge; delete both once the PR lands.
