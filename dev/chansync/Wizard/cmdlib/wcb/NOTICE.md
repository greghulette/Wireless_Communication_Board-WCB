This directory holds the WCB's own command-library boards: the verbs the WCB firmware understands that the vendored
DroidNet snapshot (`../droidnet/`) lacks or narrows.

- `wcb-maestro`: the `;M` subroutine and servo verbs at the firmware's ranges (WcbCmd `WcbMaestro.cpp`: subroutine
  0-127), plus DroidNet's own trigger for a Maestro sequence it holds by name, `@A<name>`, read on a WCB's Serial 0
  (USB). It replaces the snapshot's narrower `maestro` board, and NaviCore's inline `nc-maestro-wcb`, wherever it loads.
- `wcb-dfplayer`: the `;D` verbs (WcbCmd `WcbDfPlayer.cpp`).
- `wcb-actions`: serial text, variables (`;V,` / `;VP,`), PWM pulses (`;P`).

It is written in DroidNet's schema, so it can go upstream as it is. A few command fields are hints for the tools that
DroidNet's schema does not have; a consumer that does not know them ignores them: `defaultPort` (the serial port a new
step or action goes to: DroidNet's trigger goes to Serial 0), `laneName` and `shortName` (how the WCB Wizard's sequence
builder names its lane and its steps), and `caseInsensitive` (the device takes the fixed text in either case, so a
decoder matches it so).

**One copy, kept here in NaviCore** (`config_tool/cmdlib/wcb/`), beside the DroidNet snapshot, so the tools present the
same library: NaviCore's config tool loads it from here, Intellex bundles it with that tool, and the WCB Wizard carries a
byte-for-byte copy (`Wizard/cmdlib/wcb/`, checked by the WCB repo's `tests/wizard/unit/seq_builder.test.js`). Change it
here, then copy this directory over the Wizard's.
