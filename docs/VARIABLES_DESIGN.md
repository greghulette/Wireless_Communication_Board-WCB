# WCB Variables & Conditional Execution — Design

Dynamic, persistent variables plus a single-command `IF` gate, usable in stored
sequences, at the serial terminal, and over ESP-NOW.

---

## 1. Variables

- **Store:** NVS namespace `wcb_vars`, **RAM-mirrored** at boot. Reads (`IF`,
  `?VAR,GET`) hit RAM (instant); writes (`;V`, `?VAR,CLEAR`) update RAM **and**
  NVS.
- **Cap:** 100 variables (`WCB_MAX_VARIABLES`). Set #101 → error.
- **Type:** signed 32-bit integer. "Boolean" is just `0` / non-zero — `true`/
  `false` are input sugar for `1`/`0`. No separate type is tracked.
- **Names:** 1–15 chars (NVS key limit), **case-sensitive**, restricted to
  `[A-Za-z0-9_]`. Any other character (`, = < > ^ ; ?` space …) → rejected with
  an error at save time.
- **Undefined = 0.** `IF,x=1` on an unset `x` is false; `IF,x=0` is true.

---

## 2. Commands

### Set / mutate (runtime) — `;V`
`;V,<name>,<value-or-verb>[,<amount>]`

| Form | Effect |
|------|--------|
| `;V,name,<int>` | set to integer |
| `;V,name,0` / `,1` | set boolean |
| `;V,name,true` / `,false` | set 1 / 0 |
| `;V,name,TOGGLE` | flip: non-zero → 0, 0 → 1 |
| `;V,name,INC` / `;V,name,INC,<n>` | add n (default 1) |
| `;V,name,DEC` / `;V,name,DEC,<n>` | subtract n (default 1) |

`;V` is a `;` runtime command, so it works in sequences / serial / ESP-NOW and
respects the `IF` gate (a gated `;V` is skipped).

### Manage (config) — `?VAR`
- `?VAR,LIST` — list every variable and value
- `?VAR,SET,<name>,<value>` — create/update with an **absolute** value
  (int / `true` / `false`; no `TOGGLE`/`INC`/`DEC` — those are runtime `;V`).
  Used by the Tools-page variable manager.
- `?VAR,GET,<name>` — print one
- `?VAR,CLEAR,<name>` — delete one
- `?VAR,CLEAR,ALL` — delete all

Note: there is no separate "declare" step — the first `;V,name,value` **or**
`?VAR,SET,name,value` creates the variable.

### Conditional (runtime) — `IF`
`IF,<condition>[,AND|OR,<condition> …]`

- **Condition (inline operator):** `<name><op><literal-int>`
  Operators: `=`  `!=`  `<`  `>`  `<=`  `>=`
  e.g. `IF,domeanimations=1`, `IF,count>5`, `IF,mode>=2`
- **Compound:** conditions joined by `,AND,` / `,OR,`, evaluated **strictly
  left-to-right, no precedence / parentheses**.
  `IF,a=1,AND,b=1`  •  `IF,mode>2,OR,override=1`
- **Right-hand side is a literal integer** (no var-to-var compares in v1).
- **Gates exactly the next `^`-delimited command.** True → next command runs.
  False → next command is skipped; the rest of the chain continues.

---

## 3. Execution model (invoke-time, chain-local gate)

> **History:** v1 used a global `skipNextCommand` flag consumed inside
> `handleSingleCommand`. That design raced across command sources (an
> unrelated ESP-NOW command could be swallowed by another chain's IF), across
> queue-drain boundaries, and across timer groups — it was replaced.

IF gating is resolved **when the chain is invoked**, inside the two canonical
chain splitters with **chain-local** state (no global):

```
parseCommandsAndEnqueue / parseCommandGroups, while walking the tokens:
    bool ifSkipping = false                 # lives on the stack, per chain
    for each trimmed token:
        ifGateConsumeToken(token, ifSkipping)   # ONE shared implementation
            IF,…       -> evaluate NOW; false => start gating
            comment    -> never consumes the gate
            ;t500      -> while gating: dropped, gating CONTINUES
            ;t500,cmd  -> while gating: dropped, gate consumed
            other      -> while gating: dropped, gate consumed
        tokens that survive are enqueued / grouped normally
```

- Shared semantics live in `ifGateConsumeToken()` (WCB_Variables.cpp) so the
  timer and non-timer walks cannot drift.
- **Timers between IF and its command work**: `IF,x=1^;t500^cmd` waits then
  runs when true; skips the delay AND the command when false.
- **Consecutive IFs AND together**: `IF,a=1^IF,b=2^cmd` runs `cmd` only when
  both hold (same result as the compound `IF,a=1,AND,b=2`). Compound is clearer;
  both work.
- **IF cannot ride inside a `;t` payload** (`;t500,IF,…` → error: write
  `IF,cond^;t500^cmd`) and **cannot ride inside a `;w` payload**
  (`;w2,IF,…` → error: gate the whole route — `IF,cond^;w2,cmd`).
- **Set and test must be separate invocations.** IF evaluates at invoke time,
  before any command of the same chain has executed — so
  `;V,x,1^IF,x=1^cmd` reads the OLD value of `x` (stale-by-one). Correct
  pattern: one sequence sets the flag, another tests it:
  `?SEQ,SAVE,arm,;V,x,1` … `?SEQ,SAVE,fire,IF,x=1^cmd`.

---

## 4. Scope & per-board semantics

- Works everywhere a command can be issued (serial / ESP-NOW / sequences).
- **Variables are per-board** (each board's own NVS):
  - `;w2,;V,domeanimations,1` → sets it on **WCB2 only**
  - broadcast `;V,domeanimations,1` → sets it on **every** board
  - `IF` always tests the variable on **whichever board runs the chain**

---

## 5. Backup

`?backup` emits one `;V,<name>,<absolute-int>` line per variable, so variables
round-trip through a backup/restore and survive a reflash (not just a reboot).
Relative verbs (`TOGGLE`/`INC`/`DEC`) are never emitted — only absolute values.

---

## 6. UI

- **Wizard:** unchanged (variables aren't part of first-time setup).
- **Tools page (config tool):** a **Variables** panel per board — list, add,
  edit value, delete. `IF`/`;V` are typed directly into stored sequences; the
  sequence editor only needs to **accept `IF,…` and `;V,…` as valid** (no special
  conditional UI). They already render on their own lines (split on `^`).

---

## 7. Limits & notes

- 100 int vars ≈ ~5 KB, sharing the single 20 KB NVS partition with all config.
  Fine today; gets dedicated space when/if the 8/16 MB repartition happens.
- `;V` writes flash — don't put it inside a fast timer loop (flash wear).
- No nesting: `IF,a=1^IF,b=1^M23` does **not** mean "a AND b" (the first IF just
  gates the second IF). Use compound `IF,a=1,AND,b=1` instead.

---

## 8. Code touch-points

| File | Change |
|------|--------|
| `WCB_Variables.h/.cpp` (new) | RAM+NVS store, `;V` / `?VAR` handlers, `IF` evaluator + `ifGateConsumeToken()` (the ONE shared gate implementation), name validation, backup emit (`?VAR,SET` form) |
| `WCB.ino` | `loadVariables()` in setup; chain-local IF gating in `parseCommandsAndEnqueue` (via the shared helper); IF-in-`;w`-payload rejection in `processWCBMessage`; bare-IF safety drop in `handleSingleCommand`; `;V` case in the `;` dispatcher; `?VAR` case in `processLocalCommand`; backup emit call |
| `command_timer.cpp` | chain-local IF gating in `parseCommandGroups` (same shared helper); IF-as-timer-payload rejection; trimmed-token handling |
| `WCB_Help.cpp` | `?VAR ?`, `;V`, `IF` help blocks + overview lines |
| Wizard `app.js`/`index.html`/`parser.js` | Tools-page Variables manager; IF+command grouped on one editor line; `?VAR,SET` (and legacy `;V`) round-trips in backup parse/build |
