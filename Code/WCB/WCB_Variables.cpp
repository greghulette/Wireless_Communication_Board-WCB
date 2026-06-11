#include "WCB_RemoteTerm.h"   // must be first — redirects Serial -> WCBDebugSerial
#include "WCB_Variables.h"
#include <Preferences.h>

extern char        LocalFunctionIdentifier;
extern char        CommandCharacter;

// Dedicated NVS handle for variables. Using our OWN Preferences object (rather
// than the shared global) means our begin()/end() can never collide with a
// shared object that some other module left "started" -- Arduino's
// Preferences::begin() silently fails (returns false, no-op) if the object is
// already started, which would send our read/write to the wrong namespace.
static Preferences varPrefs;

// ---------------------------------------------------------------------------
//  RAM mirror. Sparse array indexed by slot; `used` marks live entries.
//  Persisted to NVS as a single newline-delimited "name=value" blob under
//  key "blob" in namespace "wcb_vars" (simple, no NVS iteration needed).
// ---------------------------------------------------------------------------
struct WcbVar {
  char    name[WCB_VAR_NAME_MAX + 1];
  int32_t value;
  bool    used;
};
static WcbVar vars[WCB_MAX_VARIABLES];
static int    varCount = 0;

// ---- small helpers ------------------------------------------------------
static int findVarSlot(const String &name) {            // case-sensitive exact match
  for (int i = 0; i < WCB_MAX_VARIABLES; i++)
    if (vars[i].used && name.equals(vars[i].name)) return i;
  return -1;
}
static int findFreeSlot() {
  for (int i = 0; i < WCB_MAX_VARIABLES; i++) if (!vars[i].used) return i;
  return -1;
}

// Comma-separated field, 0-based, trimmed; "" if absent.
static String vField(const String &s, int idx) {
  int start = 0;
  for (int i = 0; i < idx; i++) {
    int c = s.indexOf(',', start);
    if (c < 0) return "";
    start = c + 1;
  }
  int end = s.indexOf(',', start);
  String v = (end < 0) ? s.substring(start) : s.substring(start, end);
  v.trim();
  return v;
}

bool isValidVariableName(const String &name) {
  int n = name.length();
  if (n < 1 || n > WCB_VAR_NAME_MAX) return false;
  for (int i = 0; i < n; i++) {
    char c = name[i];
    bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || (c == '_');
    if (!ok) return false;
  }
  return true;
}

// ---- NVS persistence ----------------------------------------------------
static void saveVarsToNVS() {
  String blob;
  for (int i = 0; i < WCB_MAX_VARIABLES; i++) {
    if (!vars[i].used) continue;
    blob += vars[i].name;
    blob += '=';
    blob += String((long)vars[i].value);
    blob += '\n';
  }
  if (!varPrefs.begin("wcb_vars", false)) {
    Serial.println("[VAR] ERROR: could not open NVS (wcb_vars) to save");
    return;
  }
  size_t written = varPrefs.putString("blob", blob);
  varPrefs.end();
  if (blob.length() > 0 && written == 0)
    Serial.println("[VAR] ERROR: NVS write returned 0 bytes -- variables NOT saved");
}

void loadVariables() {
  for (int i = 0; i < WCB_MAX_VARIABLES; i++) vars[i].used = false;
  varCount = 0;

  // begin() read-only returns false if the namespace has never been written
  // (no variables saved yet) -- that's a normal first boot, not an error.
  if (!varPrefs.begin("wcb_vars", true)) {
    return;
  }
  String blob = varPrefs.getString("blob", "");
  varPrefs.end();

  int start = 0;
  while (start < (int)blob.length()) {
    int nl = blob.indexOf('\n', start);
    String line = (nl < 0) ? blob.substring(start) : blob.substring(start, nl);
    line.trim();
    if (line.length()) {
      int eq = line.indexOf('=');
      if (eq > 0) {
        String nm = line.substring(0, eq); nm.trim();
        int32_t v = (int32_t)line.substring(eq + 1).toInt();
        int slot = findFreeSlot();
        if (slot >= 0 && isValidVariableName(nm)) {
          strncpy(vars[slot].name, nm.c_str(), WCB_VAR_NAME_MAX);
          vars[slot].name[WCB_VAR_NAME_MAX] = '\0';
          vars[slot].value = v;
          vars[slot].used  = true;
          varCount++;
        }
      }
    }
    if (nl < 0) break;
    start = nl + 1;
  }
  if (varCount) Serial.printf("[VAR] Loaded %d variable(s)\n", varCount);
}

// ---- core store ---------------------------------------------------------
bool setVariable(const String &name, int32_t value) {
  if (!isValidVariableName(name)) return false;
  int idx = findVarSlot(name);
  if (idx < 0) {
    if (varCount >= WCB_MAX_VARIABLES) {
      Serial.printf("[VAR] Variable limit (%d) reached — cannot create '%s'\n",
                    WCB_MAX_VARIABLES, name.c_str());
      return false;
    }
    idx = findFreeSlot();
    if (idx < 0) return false;
    strncpy(vars[idx].name, name.c_str(), WCB_VAR_NAME_MAX);
    vars[idx].name[WCB_VAR_NAME_MAX] = '\0';
    vars[idx].used = true;
    varCount++;
  } else if (vars[idx].value == value) {
    // Unchanged — skip the NVS commit entirely. Every save rewrites the whole
    // variable blob to flash (loop() stalls ms per write + erase-cycle wear on
    // the 20 KB NVS partition), so repeated identical sets from sequences must
    // not touch flash. RAM mirror is already correct.
    return true;
  }
  vars[idx].value = value;
  saveVarsToNVS();
  return true;
}

int32_t getVariable(const String &name, int32_t defVal) {
  int idx = findVarSlot(name);
  return (idx < 0) ? defVal : vars[idx].value;
}

bool variableExists(const String &name) { return findVarSlot(name) >= 0; }

bool clearVariable(const String &name) {
  int idx = findVarSlot(name);
  if (idx < 0) return false;
  vars[idx].used = false;
  varCount--;
  saveVarsToNVS();
  return true;
}

void clearAllVariables() {
  for (int i = 0; i < WCB_MAX_VARIABLES; i++) vars[i].used = false;
  varCount = 0;
  saveVarsToNVS();
}

// ---- ;V,<name>,<value|verb>[,<amount>] ----------------------------------
void processSetVariable(const String &message) {
  String body = message; body.trim();
  // strip leading "V" / "V,"
  if (body.length() && (body[0] == 'V' || body[0] == 'v'))
    body = (body.length() > 1 && body[1] == ',') ? body.substring(2) : body.substring(1);
  body.trim();

  String name = vField(body, 0);
  String f1   = vField(body, 1);
  String f2   = vField(body, 2);

  if (!isValidVariableName(name)) {
    Serial.printf("[VAR] Invalid name '%s' — 1-%d chars, letters/digits/underscore only\n",
                  name.c_str(), WCB_VAR_NAME_MAX);
    return;
  }
  if (f1.length() == 0) {
    Serial.printf("[VAR] %cV needs a value: %cV,%s,<int|true|false|TOGGLE|INC[,n]|DEC[,n]>\n",
                  CommandCharacter, CommandCharacter, name.c_str());
    return;
  }

  String f1U = f1; f1U.toUpperCase();
  int32_t cur = getVariable(name, 0);
  int32_t newVal;

  if (f1U == "TOGGLE")      newVal = (cur != 0) ? 0 : 1;
  else if (f1U == "TRUE")   newVal = 1;
  else if (f1U == "FALSE")  newVal = 0;
  else if (f1U == "INC" || f1U == "DEC") {
    int32_t amt = (f2.length()) ? (int32_t)f2.toInt() : 1;
    newVal = (f1U == "INC") ? (cur + amt) : (cur - amt);
  } else {
    newVal = (int32_t)f1.toInt();   // integer literal (non-numeric -> 0)
  }

  if (setVariable(name, newVal))
    Serial.printf("[VAR] %s = %ld\n", name.c_str(), (long)newVal);
}

// ---- ?VAR,...  (args = text after "VAR") --------------------------------
static void listVariables() {
  Serial.println("---- Variables ----");
  if (varCount == 0) { Serial.println("  (none)"); return; }
  for (int i = 0; i < WCB_MAX_VARIABLES; i++)
    if (vars[i].used) Serial.printf("  %s = %ld\n", vars[i].name, (long)vars[i].value);
  Serial.printf("  %d/%d used\n", varCount, WCB_MAX_VARIABLES);
}

void processVarConfig(const String &args) {
  String a = args; a.trim();
  // tolerate a leading comma (?VAR,LIST -> ",LIST")
  if (a.length() && a[0] == ',') a = a.substring(1);
  a.trim();
  String aU = a; aU.toUpperCase();

  if (a.length() == 0 || aU == "LIST") { listVariables(); return; }

  // ?VAR,SET,<name>,<value>  — create/update with an absolute value.
  // (Runtime ;V also creates; this gives the ?VAR family a create/edit verb,
  //  and is what the Tools-page variable manager uses. Absolute only — no
  //  TOGGLE/INC/DEC here; those are runtime mutations via ;V.)
  if (aU.startsWith("SET")) {
    String name = vField(a, 1);
    String vStr = vField(a, 2);
    if (!isValidVariableName(name)) {
      Serial.printf("[VAR] Invalid name '%s' — 1-%d chars, letters/digits/underscore only\n",
                    name.c_str(), WCB_VAR_NAME_MAX);
      return;
    }
    if (!vStr.length()) {
      Serial.printf("[VAR] Usage: %cVAR,SET,<name>,<value>\n", LocalFunctionIdentifier);
      return;
    }
    String vU = vStr; vU.toUpperCase();
    int32_t v = (vU == "TRUE") ? 1 : (vU == "FALSE") ? 0 : (int32_t)vStr.toInt();
    if (setVariable(name, v)) Serial.printf("[VAR] %s = %ld\n", name.c_str(), (long)v);
    return;
  }

  if (aU.startsWith("GET")) {
    String name = vField(a, 1);
    if (!name.length()) { Serial.printf("[VAR] Usage: %cVAR,GET,<name>\n", LocalFunctionIdentifier); return; }
    if (!variableExists(name)) { Serial.printf("[VAR] '%s' is not set (reads as 0)\n", name.c_str()); return; }
    Serial.printf("[VAR] %s = %ld\n", name.c_str(), (long)getVariable(name, 0));
    return;
  }

  if (aU.startsWith("CLEAR")) {
    String target = vField(a, 1);
    String targetU = target; targetU.toUpperCase();
    if (targetU == "ALL") { clearAllVariables(); Serial.println("[VAR] All variables cleared"); return; }
    if (!target.length()) { Serial.printf("[VAR] Usage: %cVAR,CLEAR,<name|ALL>\n", LocalFunctionIdentifier); return; }
    if (clearVariable(target)) Serial.printf("[VAR] Cleared '%s'\n", target.c_str());
    else                       Serial.printf("[VAR] '%s' not found\n", target.c_str());
    return;
  }

  Serial.printf("[VAR] Unknown. Use: %cVAR,LIST | SET,<name>,<value> | GET,<name> | CLEAR,<name|ALL>\n",
                LocalFunctionIdentifier);
}

// ---- IF evaluator -------------------------------------------------------
// One condition: "<name><op><int>", op = =  !=  <  >  <=  >=
static bool evalOneCondition(const String &cond) {
  int p = -1; String op;
  for (int i = 0; i < (int)cond.length(); i++) {
    char c  = cond[i];
    char c2 = (i + 1 < (int)cond.length()) ? cond[i + 1] : 0;
    if (c == '<' && c2 == '=') { op = "<="; p = i; break; }
    if (c == '>' && c2 == '=') { op = ">="; p = i; break; }
    if (c == '!' && c2 == '=') { op = "!="; p = i; break; }
    if (c == '=')              { op = "=";  p = i; break; }
    if (c == '<')              { op = "<";  p = i; break; }
    if (c == '>')              { op = ">";  p = i; break; }
  }
  if (p < 0) { Serial.printf("[VAR] IF: no operator in '%s'\n", cond.c_str()); return false; }

  String name = cond.substring(0, p);            name.trim();
  String vStr = cond.substring(p + op.length()); vStr.trim();
  if (!name.length()) { Serial.printf("[VAR] IF: missing variable in '%s'\n", cond.c_str()); return false; }

  int32_t lhs = getVariable(name, 0);
  int32_t rhs = (int32_t)vStr.toInt();
  if (op == "=")  return lhs == rhs;
  if (op == "!=") return lhs != rhs;
  if (op == "<")  return lhs <  rhs;
  if (op == ">")  return lhs >  rhs;
  if (op == "<=") return lhs <= rhs;
  if (op == ">=") return lhs >= rhs;
  return false;
}

// True if a TRIMMED token is "IF,<...>" (any case). Used by the two chain
// splitters to resolve gating at invoke time — see WCB_Variables.h notes.
bool isIfChainToken(const String &trimmedTok) {
  return trimmedTok.length() >= 3 &&
         (trimmedTok[0] == 'I' || trimmedTok[0] == 'i') &&
         (trimmedTok[1] == 'F' || trimmedTok[1] == 'f') &&
         trimmedTok[2] == ',';
}

// ── Shared IF-gate token processor ────────────────────────────────────────
// THE single place that owns IF-gating semantics for one chain token. Both
// chain walkers (parseCommandsAndEnqueue in WCB.ino and parseCommandGroups in
// command_timer.cpp) call this with each TRIMMED token before enqueueing /
// pushing it, so the two walks can never drift apart again (the previous
// duplicated logic diverged on ;t and comment handling).
//
// Returns true  -> token CONSUMED by the gate (IF tokens themselves, and the
//                  tokens a false IF skips) — the walker must NOT execute it.
//         false -> process the token normally.
//
// Rules (the contract in WCB_Variables.h):
//   * IF token: evaluate now; true → drop just the IF; false → start gating.
//     An IF encountered WHILE gating = nested → error, stays gating.
//   * Comment tokens ("***...") never consume the gate — they're annotations.
//   * Pure delay tokens (";t500") under a false IF are dropped and gating
//     CONTINUES (the wait belonged to the gated command).
//   * ";t500,cmd" carries the gated action inline → dropped, gate consumed.
//   * Any other token under a false IF → dropped, gate consumed.
extern String commentDelimiter;
bool ifGateConsumeToken(const String &trimmedTok, bool &ifSkipping) {
  if (isIfChainToken(trimmedTok)) {
    if (ifSkipping) {
      Serial.printf("[IF] Nested IF is not allowed — skipped: %s\n", trimmedTok.c_str());
      return true;  // consume the inner IF, keep looking for the gated action
    }
    String cond = trimmedTok.substring(3);
    bool pass = evaluateIfCondition(cond);
    Serial.printf("[IF] %s -> %s\n", cond.c_str(),
                  pass ? "true" : "false (skipping next command)");
    ifSkipping = !pass;
    return true;
  }
  if (!ifSkipping) return false;
  if (trimmedTok.startsWith(commentDelimiter)) return false;  // annotation — walker handles it
  if (trimmedTok.length() >= 2 && trimmedTok[0] == CommandCharacter &&
      (trimmedTok[1] == 't' || trimmedTok[1] == 'T')) {
    int comma = trimmedTok.indexOf(',');
    bool hasInlineCmd = (comma != -1) && (comma + 1 < (int)trimmedTok.length());
    if (!hasInlineCmd) {
      Serial.printf("[IF] skipped delay: %s\n", trimmedTok.c_str());
      return true;  // pure delay: drop, KEEP gating
    }
  }
  ifSkipping = false;
  Serial.printf("[IF] skipped: %s\n", trimmedTok.c_str());
  return true;
}

// Full expression: "<cond>[,AND|OR,<cond> ...]"  (left-to-right, no precedence).
bool evaluateIfCondition(const String &expr) {
  String e = expr; e.trim();
  if (!e.length()) { Serial.println("[VAR] IF: empty condition"); return false; }

  bool   result    = false;
  bool   haveFirst = false;
  String pendingOp = "";   // "AND"/"OR" awaiting the next condition

  int start = 0;
  while (true) {
    int comma = e.indexOf(',', start);
    String tok = (comma < 0) ? e.substring(start) : e.substring(start, comma);
    tok.trim();
    String tokU = tok; tokU.toUpperCase();

    if (tokU == "AND" || tokU == "OR") {
      if (!haveFirst || pendingOp.length()) {
        Serial.printf("[VAR] IF: misplaced %s\n", tokU.c_str()); return false;
      }
      pendingOp = tokU;
    } else if (tok.length()) {
      bool c = evalOneCondition(tok);
      if (!haveFirst) { result = c; haveFirst = true; }
      else if (pendingOp == "AND") { result = result && c; pendingOp = ""; }
      else if (pendingOp == "OR")  { result = result || c; pendingOp = ""; }
      else {
        Serial.println("[VAR] IF: two conditions without AND/OR between them");
        return false;
      }
    }
    if (comma < 0) break;
    start = comma + 1;
  }

  if (pendingOp.length()) { Serial.printf("[VAR] IF: trailing %s\n", pendingOp.c_str()); return false; }
  if (!haveFirst)         { Serial.println("[VAR] IF: no condition"); return false; }
  return result;
}

// ---- Backup -------------------------------------------------------------
void printVariablesBackup(String &chainedConfig, String &chainedConfigDefault,
                          char delimiter, bool printToSerial,
                          const String &defSep, const String &defFunc) {
  for (int i = 0; i < WCB_MAX_VARIABLES; i++) {
    if (!vars[i].used) continue;
    // Emit as a CONFIG command (?VAR,SET,name,value), not runtime ";V":
    //  - flows through the Wizard's '^?' chain grammar and per-line parser
    //  - restores via processVarConfig exactly like every other ? entry
    // (";V" remains the runtime set/mutate command; backups are config.)
    String suffix = "VAR,SET," + String(vars[i].name) + "," + String((long)vars[i].value);
    String cmd = String(LocalFunctionIdentifier) + suffix;
    if (printToSerial) Serial.println(cmd);
    chainedConfig        += String(delimiter) + cmd;
    chainedConfigDefault += defSep + defFunc + suffix;
  }
}
