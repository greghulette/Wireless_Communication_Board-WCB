The command library the sequence builder (`../seq-builder.js`) offers: the same one NaviCore's config tool presents,
so the two tools stay in step.

| Folder | What | Kept in |
|---|---|---|
| `droidnet/` | The vendored DroidNet Command Library snapshot (its own `NOTICE.md` and MPL-2.0 `LICENSE`) | NaviCore, `config_tool/cmdlib/droidnet/` |
| `wcb/` | The WCB's own boards the snapshot lacks: Maestro at the firmware's ranges and DroidNet's `@A<name>` trigger, DFPlayer, variables, PWM, serial text | NaviCore, `config_tool/cmdlib/wcb/` |

**Both folders are copies of NaviCore's, byte for byte.** Change them in NaviCore, then copy NaviCore's
`config_tool/cmdlib/droidnet` and `config_tool/cmdlib/wcb` over these. `tests/wizard/unit/seq_builder.test.js` fails
while they differ, wherever NaviCore is checked out beside this repo.

How the builder reads, normalizes and decodes them: `docs/SEQUENCE_BUILDER.md` §4.
