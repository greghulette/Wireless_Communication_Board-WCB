#ifndef WCB_VARIABLES_H
#define WCB_VARIABLES_H

#include <Arduino.h>

// ---------------------------------------------------------------------------
// WCB user variables + conditional execution.  See VARIABLES_DESIGN.md.
//
//   Set/mutate (runtime):  ;V,<name>,<int|true|false|TOGGLE|INC[,n]|DEC[,n]>
//   Manage (config):       ?VAR,LIST | ?VAR,GET,<name> | ?VAR,CLEAR,<name|ALL>
//   Conditional (runtime): IF,<name><op><int>[,AND|OR,<name><op><int> ...]
//                          op = =  !=  <  >  <=  >=   (gates the next ^ command)
//
//   - signed 32-bit int values; "boolean" is just 0 / non-zero.
//   - names 1-15 chars, case-sensitive, [A-Za-z0-9_] only.
//   - undefined variable reads as 0.
//   - RAM-mirrored: reads hit RAM, writes update RAM + NVS (namespace wcb_vars).
// ---------------------------------------------------------------------------

#define WCB_MAX_VARIABLES   100
#define WCB_VAR_NAME_MAX     15    // NVS key length limit

// ---- Lifecycle ----------------------------------------------------------
void loadVariables();             // call once in setup(): build RAM mirror from NVS

// ---- Core store (RAM-mirrored, NVS-backed) ------------------------------
bool    setVariable(const String &name, int32_t value);   // false on bad name / table full
int32_t getVariable(const String &name, int32_t defVal = 0);
bool    variableExists(const String &name);
bool    clearVariable(const String &name);                 // false if not found
void    clearAllVariables();
bool    isValidVariableName(const String &name);           // 1-15 chars, [A-Za-z0-9_]

// ---- Command handlers ---------------------------------------------------
// ;V,<name>,<value-or-verb>[,<amount>]   (body = everything after ";V," or "V,")
void processSetVariable(const String &message);

// ?VAR,...   (args = everything after "VAR")
void processVarConfig(const String &args);

// IF condition evaluator. expr = the text after "IF," (e.g. "a=1,AND,b>3").
// Returns true  -> condition holds, run the next command.
//         false -> skip the next command.
// On a malformed expression it returns false (fail-safe: skip) and prints why.
bool evaluateIfCondition(const String &expr);

// True if a TRIMMED chain token is an IF token ("IF," any case). IF gating is
// resolved at INVOKE time inside the two canonical chain splitters
// (parseCommandsAndEnqueue / parseCommandGroups) with local walk state — there
// is deliberately NO global skip flag (cross-source/timer-boundary races).
// Semantics: IF gates the next actionable command in its own chain; pure
// delay tokens (;t500) between the IF and the command are dropped with it on
// false and preserved on true; nested IF is not allowed.
bool isIfChainToken(const String &trimmedTok);

// ---- Backup -------------------------------------------------------------
// Emits variables as "?VAR,SET,<name>,<value>" config-altitude commands (NOT
// runtime ";V" lines) so the Wizard's '^?' grammar and per-line parser see
// them. defSep/defFunc: factory-chain separator + func id (see WCB_HCR.h).
void printVariablesBackup(String &chainedConfig, String &chainedConfigDefault,
                          char delimiter, bool printToSerial = false,
                          const String &defSep = "^", const String &defFunc = "?");

#endif // WCB_VARIABLES_H
