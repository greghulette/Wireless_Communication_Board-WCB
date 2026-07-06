// ─────────────────────────────────────────────────────────────────────────────
// WCB device-label vocabulary
//
// The known droid device / accessory names the config tool suggests in its
// label comboboxes. Typing filters this list; picking from it keeps names
// consistent across droids. Free text is always still allowed — a name that
// isn't in here is accepted as-is and flagged as a candidate to add.
//
// ── TO ADD A NAME ────────────────────────────────────────────────────────────
// Just add it on its own line inside the block below. No quotes, no commas —
// one name per line. Order doesn't matter (the tool filters as you type) and
// blank lines are ignored. Keep names reasonably short so they fit the port
// label field.
// ─────────────────────────────────────────────────────────────────────────────

const WCB_DEVICE_LABELS = `
Maestro
Marcduino
Droidnet
Magic Panel
Roam A Dome Home
MP3 Trigger
HCR
Stealth
Padawan
Shadow
Shadow RC
Penumbra
Periscope
Uppity Spinner
Life Form Scanner
Leia Projector
Saber Launcher
Teeces
AstroPixels
Rseries Logics
PSI Front
PSI Rear
HP Controller
Flthy HP Controller
Data Panel/CBI
Benduino
WLED

`.trim().split('\n').map(s => s.trim()).filter(Boolean);

// Expose as a global for the classic-script Wizard (loaded before app.js).
if (typeof window !== 'undefined') window.WCB_DEVICE_LABELS = WCB_DEVICE_LABELS;
