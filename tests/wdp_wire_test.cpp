// ============================================================================
// WDP wire-format & election SPEC TEST  (host-side, no Arduino)
//
// WHAT THIS IS
//   A self-contained regression guard for the parts of the Wireless Discovery
//   Protocol that are pure logic: the TLV encode/decode, the single-owner
//   capability election, the 2-advert auto-join vetting, the sender-MAC binding
//   gate, the SOLICIT (?WDP,POLL) short-circuit, and the WDP-DA device-list frames
//   (PACKET_TYPE_WDP_DA: packing, list hash, all-or-nothing assembly, the NVS
//   blob). Runs anywhere a C++11
//   compiler exists (CI uses g++); no ESP32 / Arduino toolchain needed.
//
// WHY IT IS A REFERENCE, NOT A LINK AGAINST THE FIRMWARE
//   Code/WCB/WCB_WDP.cpp is coupled to dozens of WCB globals (maestroConfigs,
//   wledConfigs, pwmMappings, Preferences, Serial, wdpBroadcast, addActivePeer…)
//   and can't compile off-target without a large stub surface. So the wire
//   format and the election/vetting rules are re-expressed here byte-for-byte
//   from the firmware. **If you change the wire format or any rule in
//   WCB_WDP.cpp, update the mirror below and these expectations in the same
//   commit** — the value is catching accidental drift, and pinning the intended
//   behavior in something that actually executes. Firmware source anchors are
//   cited next to each mirrored piece.
//
// BUILD + RUN
//   g++ -std=c++11 -Wall -Wextra -O2 tests/wdp_wire_test.cpp -o wdp_wire_test
//   ./wdp_wire_test      # exit 0 = all pass, 1 = a failure (prints which)
// ============================================================================
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <string>
#include <vector>

// ---- constants mirrored from WCB_WDP.cpp -----------------------------------
static const uint8_t WDP_MAGIC          = 'W';
static const uint8_t WDP_PROTO_VERSION  = 0x01;
static const uint8_t T_END      = 0x00, T_ALIAS = 0x01, T_FWVER = 0x03, T_HWVER = 0x04;
static const uint8_t T_CAPFLAGS = 0x05, T_MAESTRO = 0x06, T_PORTLABEL = 0x09, T_CTRLID = 0x0A;
static const uint8_t T_DEVTYPE  = 0x0B, T_HWREV = 0x0C, T_CAPTAGS = 0x0D;
static const uint8_t T_MAESTRO_CFG = 0x0E, T_WLED_CFG = 0x0F, T_PWMTARGET = 0x10, T_SOLICIT = 0x11;
static const uint8_t T_FLAGS = 0x12, T_SEQHASH = 0x13;

static const uint16_t CAP_HCR = 0x0001, CAP_MP3 = 0x0002, CAP_WLED = 0x0004;

// WDP_BAUD_TABLE — order is the ON-WIRE code; never reorder (WCB_WDP.cpp:113).
static const uint32_t BAUD_TABLE[] = {0,110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000};
static const uint8_t  BAUD_N = sizeof(BAUD_TABLE)/sizeof(BAUD_TABLE[0]);
static uint8_t  baudToCode(uint32_t b){ for(uint8_t i=0;i<BAUD_N;i++) if(BAUD_TABLE[i]==b) return i; return 0xFF; }
static uint32_t codeToBaud(uint8_t c){ return c<BAUD_N ? BAUD_TABLE[c] : 0; }

// ---- encoder: putTLV, mirrors WCB_WDP.cpp:200 ------------------------------
static int putTLV(uint8_t*buf,int o,int max,uint8_t type,const uint8_t*val,int len){
  if(len<0) len=0;
  if(len>255) len=255;
  if(o+2+len>max) return o;
  buf[o++]=type; buf[o++]=(uint8_t)len;
  for(int i=0;i<len;i++) buf[o++]=val[i];
  return o;
}

// ---- decode target: a neighbor record (subset of WdpNeighbor) --------------
struct Neighbor {
  bool valid=false, confirmed=false, isClient=false;
  uint8_t wcbNumber=0, hwVer=0, ctrlId=0;
  uint16_t capFlags=0;
  std::string alias, fwVer, hwRev, capTags;
  uint8_t maestroIds[9]={0}, maestroBaud[9]={0}; int maestroCount=0;
  uint8_t wledIds[9]={0}, wledBaud[9]={0}; int wledCount=0;
  std::string portLabels[5];
  uint8_t pwmSelfPorts[5]={0}; int pwmSelfCount=0;
  uint32_t seqHash=0;   // T_SEQHASH; 0 = not advertised (an EMPTY set is 0x811C9DC5)
};

// Result of feeding a packet to the decoder.
enum DecodeKind { DK_REJECTED, DK_SOLICIT, DK_ADVERT };

// Mirrors wdpOnAdvertReceived (WCB_WDP.cpp:431+): the two inbound gates that are
// pure (magic/version + sender↔MAC binding), the SOLICIT short-circuit that must
// run BEFORE the record is wiped, then the TLV switch that rebuilds the record.
static DecodeKind decode(int senderWCB, uint8_t srcMacLastOctet,
                         const uint8_t* cmd, int cmdLen, int selfWCB, Neighbor& nb) {
  (void)cmdLen;
  if (senderWCB < 1 || senderWCB > 20) return DK_REJECTED;
  if (cmd[0] != WDP_MAGIC || cmd[1] != WDP_PROTO_VERSION) return DK_REJECTED;
  // sender-MAC binding (WCB_WDP.cpp §8): claimed id must equal the source MAC's
  // last octet. A rogue can't claim another board's id.
  if (srcMacLastOctet != (uint8_t)senderWCB) return DK_REJECTED;

  // SOLICIT short-circuit BEFORE any wipe (WCB_WDP.cpp:434+ new block).
  { int s=2;
    while(s+2<=200){
      uint8_t ty=cmd[s];
      if(ty==T_END) break;
      int ln=cmd[s+1];
      if(s+2+ln>200) break;
      if(ty==T_SOLICIT) return DK_SOLICIT;
      s+=2+ln;
    } }

  nb = Neighbor();                       // memset-equivalent wholesale replace
  nb.valid=true; nb.confirmed=true; nb.wcbNumber=(uint8_t)senderWCB;

  int o=2;
  while(o+2<=200){
    uint8_t type=cmd[o]; if(type==T_END) break;
    int len=cmd[o+1]; const uint8_t*val=&cmd[o+2];
    if(o+2+len>200) break;               // truncated / malformed — stop safely
    switch(type){
      case T_ALIAS:   nb.alias.assign((const char*)val, len>24?24:len); break;
      case T_FWVER:   nb.fwVer.assign((const char*)val, len>27?27:len); break;
      case T_HWVER:   if(len>=1) nb.hwVer=val[0]; break;
      case T_CAPFLAGS:if(len>=2) nb.capFlags=(uint16_t)val[0]|((uint16_t)val[1]<<8); break;
      case T_CTRLID:  if(len>=1) nb.ctrlId=val[0]; break;
      case T_DEVTYPE: nb.alias.assign((const char*)val, len>24?24:len); nb.isClient=true; break;
      case T_HWREV:   nb.hwRev.assign((const char*)val, len>15?15:len); break;
      case T_CAPTAGS: nb.capTags.assign((const char*)val, len>48?48:len); break;
      case T_MAESTRO: { int L=len>9?9:len; for(int i=0;i<L;i++){nb.maestroIds[i]=val[i]; nb.maestroBaud[i]=0xFF;} nb.maestroCount=L; break; }
      case T_MAESTRO_CFG: { int r=len/2; if(r>9)r=9; for(int i=0;i<r;i++){nb.maestroIds[i]=val[i*2]; nb.maestroBaud[i]=val[i*2+1];} nb.maestroCount=r; break; }
      case T_WLED_CFG: { int r=len/2; if(r>9)r=9; for(int i=0;i<r;i++){nb.wledIds[i]=val[i*2]; nb.wledBaud[i]=val[i*2+1];} nb.wledCount=r; break; }
      case T_PORTLABEL: if(len>=1){ int p=val[0]; if(p>=1&&p<=5) nb.portLabels[p-1].assign((const char*)(val+1), len-1);} break;
      case T_PWMTARGET: { int r=len/2; for(int i=0;i<r;i++){ uint8_t tgt=val[i*2], prt=val[i*2+1];
            if(tgt!=(uint8_t)selfWCB) continue;
            if(prt<1||prt>5) continue;
            bool dup=false; for(int j=0;j<nb.pwmSelfCount;j++) if(nb.pwmSelfPorts[j]==prt) dup=true;
            if(!dup && nb.pwmSelfCount<5) nb.pwmSelfPorts[nb.pwmSelfCount++]=prt; } break; }
      case T_SEQHASH: if(len>=4) nb.seqHash=(uint32_t)val[0]|((uint32_t)val[1]<<8)|
                                            ((uint32_t)val[2]<<16)|((uint32_t)val[3]<<24); break;
      default: break;                    // unknown TLV — skipped via length (forward compatible)
    }
    o += 2 + len;
  }
  return DK_ADVERT;
}

// ---- capability election, mirrors wdpCapOwner (WCB_WDP.cpp:381) ------------
// Lowest-numbered board advertising capBit that is ALSO online; self included
// (self is always "online"). 0 if nobody — not even self — owns it.
static int capOwner(uint16_t capBit, int selfWCB, uint16_t selfCaps,
                    const std::vector<Neighbor>& tbl, const bool online[21]) {
  int owner = 0;
  if (selfCaps & capBit) owner = selfWCB;
  for (const auto& nb : tbl) {
    if (!nb.valid || nb.isClient) continue;
    if (!(nb.capFlags & capBit)) continue;
    if (!online[nb.wcbNumber]) continue;
    if (owner == 0 || nb.wcbNumber < owner) owner = nb.wcbNumber;
  }
  return owner;
}

// ============================ test harness ==================================
static int g_fail = 0;
#define CHECK(cond, msg) do{ if(!(cond)){ printf("  FAIL: %s\n", msg); g_fail++; } }while(0)

static void test_roundtrip(){
  printf("roundtrip encode/decode...\n");
  uint8_t buf[200]; memset(buf,0,sizeof(buf));
  int o=0; buf[o++]=WDP_MAGIC; buf[o++]=WDP_PROTO_VERSION;
  o=putTLV(buf,o,200,T_ALIAS,(const uint8_t*)"DomeBoard",9);
  o=putTLV(buf,o,200,T_FWVER,(const uint8_t*)"9.9.9",5);
  { uint8_t hw=31; o=putTLV(buf,o,200,T_HWVER,&hw,1); }
  { uint8_t cf[2]={ (uint8_t)((CAP_HCR|CAP_WLED)&0xFF), (uint8_t)(((CAP_HCR|CAP_WLED)>>8)&0xFF) };
    o=putTLV(buf,o,200,T_CAPFLAGS,cf,2); }
  { uint8_t m[4]={1,baudToCode(57600),2,baudToCode(115200)}; o=putTLV(buf,o,200,T_MAESTRO_CFG,m,4); }
  { uint8_t w[2]={5,baudToCode(115200)}; o=putTLV(buf,o,200,T_WLED_CFG,w,2); }
  { uint8_t ct=7; o=putTLV(buf,o,200,T_CTRLID,&ct,1); }
  { uint8_t pl[5]={2,'H','P','1','0'}; o=putTLV(buf,o,200,T_PORTLABEL,pl,5); }
  buf[o++]=T_END;

  Neighbor nb;
  CHECK(decode(3,3,buf,o,4,nb)==DK_ADVERT, "advert accepted");
  CHECK(nb.wcbNumber==3 && nb.valid && nb.confirmed, "identity");
  CHECK(nb.alias=="DomeBoard", "alias");
  CHECK(nb.fwVer=="9.9.9", "fwver");
  CHECK(nb.hwVer==31, "hwver");
  CHECK(nb.capFlags==(CAP_HCR|CAP_WLED), "capflags little-endian");
  CHECK(nb.maestroCount==2 && nb.maestroIds[0]==1 && codeToBaud(nb.maestroBaud[0])==57600, "maestro id+baud");
  CHECK(nb.maestroIds[1]==2 && codeToBaud(nb.maestroBaud[1])==115200, "maestro #2 baud");
  CHECK(nb.wledCount==1 && nb.wledIds[0]==5 && codeToBaud(nb.wledBaud[0])==115200, "wled id+baud");
  CHECK(nb.ctrlId==7, "ctrlid");
  CHECK(nb.portLabels[1]=="HP10", "portlabel S2");
}

static void test_unknown_and_truncated(){
  printf("unknown TLV skip + truncation safety...\n");
  uint8_t buf[200]; memset(buf,0,sizeof(buf));
  int o=0; buf[o++]=WDP_MAGIC; buf[o++]=WDP_PROTO_VERSION;
  { uint8_t x[3]={9,9,9}; o=putTLV(buf,o,200,0x33,x,3); }        // unknown future TLV
  o=putTLV(buf,o,200,T_ALIAS,(const uint8_t*)"After",5);         // must still be read
  buf[o++]=T_END;
  Neighbor nb;
  CHECK(decode(4,4,buf,o,1,nb)==DK_ADVERT && nb.alias=="After", "unknown TLV skipped by length");

  // A TLV whose length runs past the field must stop the loop, not read OOB.
  uint8_t bad[200]; memset(bad,0,sizeof(bad));
  int b=0; bad[b++]=WDP_MAGIC; bad[b++]=WDP_PROTO_VERSION;
  bad[b++]=T_ALIAS; bad[b++]=250;                                // claims 250 bytes near the end
  Neighbor nb2;
  CHECK(decode(4,4,bad,b,1,nb2)==DK_ADVERT && nb2.alias.empty(), "over-long TLV stops decode safely");
}

// T_SEQHASH (0x13) — the stored-sequence inventory fingerprint that tells a
// consumer WHEN to re-pull sequence names (?MGMT,SEQ,<n> / requestSequenceNames).
// The names themselves are deliberately NOT on the wire: ~16 B each would evict
// the port labels from the fixed 200 B payload.
static void test_seqhash(){
  printf("seqhash TLV round-trip + absence semantics...\n");
  uint8_t buf[200]; memset(buf,0,sizeof(buf));
  int o=0; buf[o++]=WDP_MAGIC; buf[o++]=WDP_PROTO_VERSION;
  // 0x811C9DC5 = FNV-1a offset basis = the hash of an EMPTY key_list. Chosen here
  // deliberately: it is the value most likely to be confused with "no hash", and
  // it must survive the round-trip as a real, non-zero reading.
  const uint32_t H = 0x811C9DC5u;
  { uint8_t v[4]={(uint8_t)(H&0xFF),(uint8_t)((H>>8)&0xFF),
                  (uint8_t)((H>>16)&0xFF),(uint8_t)((H>>24)&0xFF)};   // little-endian
    o=putTLV(buf,o,200,T_SEQHASH,v,4); }
  o=putTLV(buf,o,200,T_ALIAS,(const uint8_t*)"DomeBoard",9);
  buf[o++]=T_END;
  Neighbor nb;
  CHECK(decode(4,4,buf,o,1,nb)==DK_ADVERT, "advert with seqhash decodes");
  CHECK(nb.seqHash==H, "seqhash little-endian round-trip");
  CHECK(nb.alias=="DomeBoard", "TLVs after seqhash still parse");

  // An advert with NO seqhash must leave it 0 — that is how a consumer tells
  // "firmware predates the TLV" from "board has zero sequences" (which is H).
  uint8_t old[200]; memset(old,0,sizeof(old));
  int p=0; old[p++]=WDP_MAGIC; old[p++]=WDP_PROTO_VERSION;
  p=putTLV(old,p,200,T_ALIAS,(const uint8_t*)"OldFw",5);
  old[p++]=T_END;
  Neighbor nb2;
  CHECK(decode(4,4,old,p,1,nb2)==DK_ADVERT && nb2.seqHash==0, "absent seqhash reads 0, not the empty-set hash");

  // A short/corrupt SEQHASH must be ignored rather than half-applied.
  uint8_t shortH[200]; memset(shortH,0,sizeof(shortH));
  int s=0; shortH[s++]=WDP_MAGIC; shortH[s++]=WDP_PROTO_VERSION;
  { uint8_t v2[2]={0xC5,0x9D}; s=putTLV(shortH,s,200,T_SEQHASH,v2,2); }
  s=putTLV(shortH,s,200,T_ALIAS,(const uint8_t*)"Short",5);
  shortH[s++]=T_END;
  Neighbor nb3;
  CHECK(decode(4,4,shortH,s,1,nb3)==DK_ADVERT && nb3.seqHash==0 && nb3.alias=="Short",
        "under-length seqhash ignored, later TLVs still parse");
}

static void test_maestro_cfg_supersedes(){
  printf("MAESTRO_CFG supersedes id-only MAESTRO...\n");
  uint8_t buf[200]; memset(buf,0,sizeof(buf));
  int o=0; buf[o++]=WDP_MAGIC; buf[o++]=WDP_PROTO_VERSION;
  { uint8_t m[2]={1,2}; o=putTLV(buf,o,200,T_MAESTRO,m,2); }          // legacy id-only (baud unknown)
  { uint8_t mc[4]={1,baudToCode(57600),2,baudToCode(9600)}; o=putTLV(buf,o,200,T_MAESTRO_CFG,mc,4); }
  buf[o++]=T_END;
  Neighbor nb; decode(5,5,buf,o,1,nb);
  CHECK(nb.maestroCount==2 && codeToBaud(nb.maestroBaud[0])==57600 && codeToBaud(nb.maestroBaud[1])==9600,
        "rich cfg wins over id-only");
}

static void test_pwmtarget_self_only(){
  printf("PWMTARGET keeps only self-named ports (deduped)...\n");
  uint8_t buf[200]; memset(buf,0,sizeof(buf));
  int o=0; buf[o++]=WDP_MAGIC; buf[o++]=WDP_PROTO_VERSION;
  // sender drives: WCB2 S3, WCB4 S1 (us), WCB4 S1 (dup), WCB4 S5, WCB7 S2.
  // With self=WCB4, only S1 and S5 survive; the foreign S3/S2 and the S1 dup drop.
  uint8_t rec[10]={2,3, 4,1, 4,1, 4,5, 7,2};
  o=putTLV(buf,o,200,T_PWMTARGET,rec,10); buf[o++]=T_END;
  Neighbor nb; decode(6,6,buf,o,/*selfWCB=*/4,nb);
  bool has1=false, has5=false, has3=false;
  for(int i=0;i<nb.pwmSelfCount;i++){ if(nb.pwmSelfPorts[i]==1)has1=true; if(nb.pwmSelfPorts[i]==5)has5=true; if(nb.pwmSelfPorts[i]==3)has3=true; }
  CHECK(nb.pwmSelfCount==2 && has1 && has5 && !has3, "self=WCB4 keeps S1,S5 only (dedup, no foreign S3)");
}

static void test_solicit(){
  printf("SOLICIT short-circuit (no record wipe)...\n");
  uint8_t buf[8]; int o=0; buf[o++]=WDP_MAGIC; buf[o++]=WDP_PROTO_VERSION;
  buf[o++]=T_SOLICIT; buf[o++]=0; buf[o++]=T_END;
  Neighbor nb; nb.valid=true; nb.alias="pre-existing";     // must NOT be wiped
  CHECK(decode(2,2,buf,o,1,nb)==DK_SOLICIT, "solicit detected");
  CHECK(nb.alias=="pre-existing" && nb.valid, "solicit did not clobber the neighbor record");
}

static void test_mac_binding(){
  printf("sender-MAC binding gate...\n");
  uint8_t buf[16]; int o=0; buf[o++]=WDP_MAGIC; buf[o++]=WDP_PROTO_VERSION;
  o=putTLV(buf,o,16,T_ALIAS,(const uint8_t*)"X",1); buf[o++]=T_END;
  Neighbor nb;
  CHECK(decode(3,/*mac=*/9,buf,o,1,nb)==DK_REJECTED, "id!=MAC rejected (anti-spoof)");
  CHECK(decode(3,/*mac=*/3,buf,o,1,nb)==DK_ADVERT,   "id==MAC accepted");
}

static void test_cap_election(){
  printf("single-owner capability election...\n");
  std::vector<Neighbor> tbl(3);
  tbl[0].valid=true; tbl[0].wcbNumber=5; tbl[0].capFlags=CAP_HCR;   // online below
  tbl[1].valid=true; tbl[1].wcbNumber=2; tbl[1].capFlags=CAP_HCR;   // lowest, but OFFLINE in case B
  tbl[2].valid=true; tbl[2].wcbNumber=8; tbl[2].capFlags=CAP_MP3;
  bool online[21]; for(int i=0;i<21;i++) online[i]=true;

  // self (WCB4) has no caps → lowest online HCR board = WCB2
  CHECK(capOwner(CAP_HCR,4,0,tbl,online)==2, "lowest online owner (WCB2)");
  // WCB2 offline → falls to next online HCR owner WCB5
  online[2]=false;
  CHECK(capOwner(CAP_HCR,4,0,tbl,online)==5, "offline lowest skipped → WCB5");
  // self advertises HCR and is lower than 5 → self wins (self always online)
  CHECK(capOwner(CAP_HCR,4,CAP_HCR,tbl,online)==4, "self included and wins when lower");
  // nobody owns WLED (no neighbor, self none) → 0
  CHECK(capOwner(CAP_WLED,4,0,tbl,online)==0, "nobody owns → 0");
  // client boards never count even if capFlags set
  tbl[0].isClient=true; online[2]=false;
  CHECK(capOwner(CAP_HCR,4,0,tbl,online)==0, "client-with-cap ignored, WCB2 offline → 0");
}

static void test_autojoin_vetting(){
  printf("auto-join needs >=2 adverts...\n");
  // Mirror the wcbPeerAdvertCount>=2 gate (WCB_WDP.cpp:573): joining only after a
  // second advert so one stray/echoed packet can't inject a peer.
  int advertCount=0; bool joined=false;
  auto onAdvert=[&](){ if(++advertCount>=2) joined=true; };
  onAdvert(); CHECK(!joined, "one advert does not join");
  onAdvert(); CHECK(joined,  "second advert joins");
}

// ============================ WDP-DA device-list frames =====================
// PACKET_TYPE_WDP_DA (17): a board's serial-attached devices, sent to every neighbor
// after each advert and when the list changes. Mirrors the WDP-DA section of
// WCB_WDP.cpp: wdpDaPutRecord / wdpDaGetRecord (the record, shared with the NVS blob),
// wdpDaBuildFrame (greedy packing + list hash), wdpDaOnFrameReceived (all-or-nothing
// assembly into a 32-record shared pool) and wdpDaSave / wdpDaLoad (the blob).
static const uint8_t  DA_MAGIC = 'D', DA_VERSION = 0x01, DA_NVS_VERSION = 0x01;
static const uint8_t  DA_FLAG_LIVE = 0x01;
static const int      DA_HDR = 9, DA_PER_PORT = 4, DA_MAX_FRAMES = 5 * 4, DA_POOL = 32;
static const int      DA_REC_MAX = 6 + 24 + 27 + 15 + 48;

struct DaRec { uint8_t port = 1, flags = 0; std::string type, fw, hw, caps; };

static void daScrub(std::string& s){ for(auto& c : s) if(c==','||c==']'||(uint8_t)c<0x20) c='_'; }

static int daPutStr(uint8_t* buf, int o, const std::string& s, int cap){
  int L = (int)s.size() < cap ? (int)s.size() : cap;
  buf[o++] = (uint8_t)L; memcpy(buf + o, s.data(), L); return o + L;
}
static int daPutRecord(uint8_t* buf, int o, int max, const DaRec& r){
  auto cl=[](const std::string& s,int c){ return (int)s.size()<c?(int)s.size():c; };
  int need = 6 + cl(r.type,24) + cl(r.fw,27) + cl(r.hw,15) + cl(r.caps,48);
  if(o + need > max) return -1;
  buf[o++] = r.port; buf[o++] = r.flags;
  o = daPutStr(buf,o,r.type,24); o = daPutStr(buf,o,r.fw,27); o = daPutStr(buf,o,r.hw,15);
  return daPutStr(buf,o,r.caps,48);
}
static int daGetStr(const uint8_t* buf, int o, int max, std::string& out, int cap){
  if(o >= max) return -1;
  int L = buf[o++]; if(L > cap || o + L > max) return -1;
  out.assign((const char*)buf + o, L); return o + L;
}
static int daGetRecord(const uint8_t* buf, int o, int max, DaRec& r){
  if(o + 2 > max) return -1;
  r.port = buf[o++]; r.flags = buf[o++];
  if(r.port < 1 || r.port > 5) return -1;
  if((o = daGetStr(buf,o,max,r.type,24)) < 0 || r.type.empty()) return -1;
  if((o = daGetStr(buf,o,max,r.fw,27))   < 0) return -1;
  if((o = daGetStr(buf,o,max,r.hw,15))   < 0) return -1;
  if((o = daGetStr(buf,o,max,r.caps,48)) < 0) return -1;
  daScrub(r.type); daScrub(r.fw); daScrub(r.hw); daScrub(r.caps);
  return o;
}

// wdpDaBuildFrame: `list` is already in list order (port by port, first-heard first).
static int daBuildFrame(const std::vector<DaRec>& list, int frameIdx, uint8_t* buf,
                        uint32_t* hash, int* frames, int* records){
  uint8_t rec[DA_REC_MAX]; uint32_t h = 2166136261u;
  int nFrames = 1, used = DA_HDR, len = 0, n = 0;
  memset(buf, 0, 200);
  for(const auto& r : list){
    int rl = daPutRecord(rec, 0, sizeof(rec), r);
    for(int i=0;i<rl;i++){ h ^= rec[i]; h *= 16777619u; }
    if(used + rl > 200 - 1){ nFrames++; used = DA_HDR; }
    if(nFrames - 1 == frameIdx){ memcpy(buf + used, rec, rl); len = used + rl; }
    used += rl; n++;
  }
  h ^= (uint8_t)n; h *= 16777619u;
  *hash = h; *frames = nFrames; *records = n;
  if(frameIdx >= nFrames) return 0;
  if(len == 0) len = DA_HDR;
  buf[0]=DA_MAGIC; buf[1]=DA_VERSION;
  buf[2]=(uint8_t)(h&0xFF); buf[3]=(uint8_t)((h>>8)&0xFF); buf[4]=(uint8_t)((h>>16)&0xFF); buf[5]=(uint8_t)((h>>24)&0xFF);
  buf[6]=(uint8_t)frameIdx; buf[7]=(uint8_t)nFrames; buf[8]=(uint8_t)n;
  return len;
}
static std::vector<std::vector<uint8_t>> daAllFrames(const std::vector<DaRec>& list, uint32_t* hashOut = nullptr){
  std::vector<std::vector<uint8_t>> out; uint8_t buf[200]; uint32_t h; int frames, recs;
  daBuildFrame(list, 0, buf, &h, &frames, &recs);
  for(int i=0;i<frames;i++){ daBuildFrame(list, i, buf, &h, &frames, &recs); out.emplace_back(buf, buf + 200); }
  if(hashOut) *hashOut = h;
  return out;
}

// Receiver: wdpDaOnFrameReceived + its pool/peer state.
struct DaRemote { uint8_t wcb = 0; bool pending = false; uint16_t pos = 0; DaRec r; };
struct DaPeer { bool have=false; uint32_t hash=0; bool assembling=false; uint32_t asmHash=0;
                uint8_t asmFrames=0, asmRecords=0; uint32_t asmMask=0; };
struct DaRx {
  DaRemote pool[DA_POOL]; DaPeer peers[21];
  void freeRemote(uint8_t w, bool pending){ for(auto& e : pool) if(e.wcb==w && e.pending==pending) e.wcb=0; }
  void abortAsm(uint8_t w){ freeRemote(w,true); peers[w].assembling=false; }
  void onFrame(int sender, const uint8_t* cmd){
    if(sender < 1 || sender > 20) return;
    if(cmd[0] != DA_MAGIC || cmd[1] != DA_VERSION) return;
    uint32_t hash = (uint32_t)cmd[2]|((uint32_t)cmd[3]<<8)|((uint32_t)cmd[4]<<16)|((uint32_t)cmd[5]<<24);
    uint8_t idx = cmd[6], frames = cmd[7], records = cmd[8];
    if(frames == 0 || frames > DA_MAX_FRAMES || idx >= frames) return;
    if(records > 5 * DA_PER_PORT) return;
    uint8_t w = (uint8_t)sender; DaPeer& pe = peers[w];
    if(pe.have && pe.hash == hash) return;
    if(!pe.assembling || pe.asmHash != hash || pe.asmFrames != frames || pe.asmRecords != records){
      freeRemote(w,true); pe.assembling=true; pe.asmHash=hash; pe.asmFrames=frames; pe.asmRecords=records; pe.asmMask=0;
    }
    if(pe.asmMask & (1UL << idx)) return;
    int o = DA_HDR, n = 0;
    while(o < 200 && cmd[o] != 0){
      DaRec r; int next = daGetRecord(cmd, o, 200, r);
      if(next < 0){ abortAsm(w); return; }
      o = next;
      int slot = -1; for(int i=0;i<DA_POOL;i++) if(pool[i].wcb==0){ slot=i; break; }
      if(slot < 0){ abortAsm(w); return; }
      pool[slot].wcb=w; pool[slot].pending=true; pool[slot].pos=(uint16_t)(idx*32+n); pool[slot].r=r; n++;
    }
    pe.asmMask |= (1UL << idx);
    if(pe.asmMask != ((1UL << frames) - 1)) return;
    int got = 0; for(auto& e : pool) if(e.wcb==w && e.pending) got++;
    if(got != records){ abortAsm(w); return; }
    freeRemote(w,false);
    for(auto& e : pool) if(e.wcb==w) e.pending=false;
    pe.have=true; pe.hash=hash; pe.assembling=false;
  }
  std::vector<DaRec> list(uint8_t w) const {   // wdpDaRemoteList: committed, by pos
    std::vector<const DaRemote*> v;
    for(const auto& e : pool) if(e.wcb==w && !e.pending) v.push_back(&e);
    for(size_t i=1;i<v.size();i++) for(size_t j=i;j>0 && v[j-1]->pos > v[j]->pos;j--) std::swap(v[j-1],v[j]);
    std::vector<DaRec> out; for(auto* e : v) out.push_back(e->r); return out;
  }
};

static DaRec daRec(uint8_t port, const char* type, const char* fw = "", bool live = true,
                   const char* hw = "", const char* caps = ""){
  DaRec r; r.port=port; r.flags=live?DA_FLAG_LIVE:0; r.type=type; r.fw=fw; r.hw=hw; r.caps=caps; return r;
}
static bool daSame(const std::vector<DaRec>& a, const std::vector<DaRec>& b){
  if(a.size()!=b.size()) return false;
  for(size_t i=0;i<a.size();i++)
    if(a[i].port!=b[i].port||a[i].flags!=b[i].flags||a[i].type!=b[i].type||a[i].fw!=b[i].fw||
       a[i].hw!=b[i].hw||a[i].caps!=b[i].caps) return false;
  return true;
}
// A record with every field at full length: 120 bytes, so each gets a frame to itself.
static DaRec daLong(uint8_t port, int i){
  char t[25]; snprintf(t, sizeof(t), "Type%02d-AAAAAAAAAAAAAAAAA", i);
  return daRec(port, t, "FFFFFFFFFFFFFFFFFFFFFFFFFFF", true, "HHHHHHHHHHHHHHH",
               "cccccccccccccccccccccccccccccccccccccccccccccccc");
}

static void test_da_single_frame(){
  printf("WDP-DA: one-frame list round-trip...\n");
  std::vector<DaRec> L = { daRec(2,"PSI Front","1.4"), daRec(2,"PSI Rear","1.4",false),
                           daRec(4,"Flthy HP Controller","2.3.0",true,"revB","hp.servo hp.led") };
  uint32_t h; auto F = daAllFrames(L, &h);
  CHECK(F.size()==1, "three short records fit one frame");
  CHECK(F[0][0]==DA_MAGIC && F[0][7]==1 && F[0][8]==3, "header: magic, 1 frame, 3 records");
  DaRx rx; rx.onFrame(3, F[0].data());
  CHECK(daSame(rx.list(3), L), "list, order and live flags survive");
  CHECK(rx.peers[3].have && rx.peers[3].hash==h, "committed under the sender's list hash");
}

static void test_da_multi_frame_any_order(){
  printf("WDP-DA: multi-frame list commits only when whole, in any order...\n");
  std::vector<DaRec> L; for(int i=0;i<20;i++) L.push_back(daLong((uint8_t)(1 + i/4), i));
  auto F = daAllFrames(L);
  CHECK(F.size()==20, "20 full-length records need 20 frames");
  bool termOk = true; for(auto& f : F){ int o=DA_HDR; DaRec r; o=daGetRecord(f.data(),o,200,r); if(o<0||o>=200||f[o]!=0) termOk=false; }
  CHECK(termOk, "each frame ends with a 0 port byte inside the 200 B");
  DaRx rx;
  for(int i=19;i>=1;i--) rx.onFrame(5, F[i].data());
  CHECK(rx.list(5).empty() && !rx.peers[5].have, "nothing shown until the last frame");
  rx.onFrame(5, F[0].data());
  CHECK(daSame(rx.list(5), L), "reverse-order delivery assembles the list in list order");
}

static void test_da_lost_frame_keeps_old(){
  printf("WDP-DA: a lost frame leaves the previous list, never half a new one...\n");
  std::vector<DaRec> A = { daRec(3,"Rseries Logics","2.1") };
  std::vector<DaRec> B; for(int i=0;i<3;i++) B.push_back(daLong(3, i));
  DaRx rx; rx.onFrame(4, daAllFrames(A)[0].data());
  auto FB = daAllFrames(B);
  CHECK(FB.size()==3, "B spans three frames");
  rx.onFrame(4, FB[0].data()); rx.onFrame(4, FB[2].data());          // frame 1 lost
  CHECK(daSame(rx.list(4), A), "old list still shown while B is incomplete");
  rx.onFrame(4, FB[0].data());                                          // re-send: duplicate ignored...
  rx.onFrame(4, FB[1].data());                                          // ...and the missing one completes it
  CHECK(daSame(rx.list(4), B), "the re-send completes B");
  int used = 0; for(auto& e : rx.pool) if(e.wcb) used++;
  CHECK(used==3, "A's slots were freed when B committed");
}

static void test_da_empty_and_repeat(){
  printf("WDP-DA: an empty list clears; a repeat of the same list is ignored...\n");
  std::vector<DaRec> A = { daRec(1,"Maestro","1.0") };
  DaRx rx; rx.onFrame(6, daAllFrames(A)[0].data());
  auto F0 = daAllFrames(A)[0];
  rx.onFrame(6, F0.data());
  CHECK(daSame(rx.list(6), A) && !rx.peers[6].assembling, "same hash again: no re-assembly");
  std::vector<DaRec> none; auto FE = daAllFrames(none);
  CHECK(FE.size()==1 && FE[0][8]==0 && FE[0][DA_HDR]==0, "empty list = one header-only frame");
  rx.onFrame(6, FE[0].data());
  CHECK(rx.list(6).empty() && rx.peers[6].have, "empty list commits and clears the old one");
}

static void test_da_hash_tracks_state(){
  printf("WDP-DA: the list hash moves with any change, incl. the live flag...\n");
  uint32_t h1, h2, h3, h4;
  daAllFrames({ daRec(2,"PSI Front","1.4",true) }, &h1);
  daAllFrames({ daRec(2,"PSI Front","1.4",false) }, &h2);
  daAllFrames({ daRec(2,"PSI Front","1.5",true) }, &h3);
  daAllFrames({ daRec(3,"PSI Front","1.4",true) }, &h4);
  CHECK(h1!=h2, "going quiet changes the hash (neighbors see it)");
  CHECK(h1!=h3 && h1!=h4, "a new fw or a different port changes the hash");
}

static void test_da_malformed(){
  printf("WDP-DA: malformed frames are dropped whole...\n");
  std::vector<DaRec> A = { daRec(2,"PSI Front","1.4") };
  auto F = daAllFrames(A)[0];
  DaRx rx;
  { auto f = F; f[DA_HDR] = 9;  rx.onFrame(2, f.data()); CHECK(rx.list(2).empty(), "port 9 rejected"); }
  { auto f = F; f[DA_HDR+2] = 30; rx.onFrame(2, f.data()); CHECK(rx.list(2).empty(), "type length past its field rejected"); }
  { auto f = F; f[DA_HDR+2] = 0;  rx.onFrame(2, f.data()); CHECK(rx.list(2).empty(), "empty type rejected"); }
  { auto f = F; f[7] = 0; rx.onFrame(2, f.data()); CHECK(rx.list(2).empty() && !rx.peers[2].assembling, "zero frame count rejected"); }
  { auto f = F; f[6] = 1; rx.onFrame(2, f.data()); CHECK(rx.list(2).empty() && !rx.peers[2].assembling, "index past the count rejected"); }
  { auto f = F; f[8] = 2; rx.onFrame(2, f.data()); CHECK(rx.list(2).empty() && !rx.peers[2].have, "record count mismatch never commits"); }
  { auto f = F; f[0] = 'W'; rx.onFrame(2, f.data()); CHECK(!rx.peers[2].assembling, "an advert's magic is not a device list"); }
  int used = 0; for(auto& e : rx.pool) if(e.wcb) used++;
  CHECK(used==0, "no pool slot leaked by a rejected frame");
  rx.onFrame(2, F.data());
  CHECK(daSame(rx.list(2), A), "the intact frame still commits afterwards");
}

static void test_da_scrub(){
  printf("WDP-DA: received strings are scrubbed for the dump...\n");
  std::vector<DaRec> A = { daRec(1,"PSI,Front]","1\x01" "4") };
  DaRx rx; rx.onFrame(7, daAllFrames(A)[0].data());
  auto L = rx.list(7);
  CHECK(L.size()==1 && L[0].type=="PSI_Front_" && L[0].fw=="1_4", "',' ']' and control bytes become '_'");
}

static void test_da_pool_full(){
  printf("WDP-DA: a list that doesn't fit the shared pool leaves the old one...\n");
  DaRx rx;
  std::vector<DaRec> A; for(int i=0;i<20;i++) A.push_back(daRec((uint8_t)(1 + i/4), ("A" + std::to_string(i)).c_str()));
  std::vector<DaRec> B; for(int i=0;i<20;i++) B.push_back(daRec((uint8_t)(1 + i/4), ("B" + std::to_string(i)).c_str()));
  for(auto& f : daAllFrames(A)) rx.onFrame(8, f.data());
  CHECK(rx.list(8).size()==20, "first neighbor's 20 records fit");
  for(auto& f : daAllFrames(B)) rx.onFrame(9, f.data());
  CHECK(rx.list(9).empty() && !rx.peers[9].assembling, "second 20 don't fit the 32-slot pool: dropped");
  CHECK(rx.list(8).size()==20, "the first neighbor's list is untouched");
  int used = 0; for(auto& e : rx.pool) if(e.wcb) used++;
  CHECK(used==20, "the partial list's slots were freed");
}

// wdpDaSave / wdpDaLoad: ['D'][version][count] + records, list order; load skips a
// duplicate type on a port and anything past 4 per port, and keeps a corrupt blob's head.
static std::vector<DaRec> daLoad(const std::vector<uint8_t>& blob){
  std::vector<DaRec> out; int perPort[6] = {0};
  if(blob.size() < 3 || blob[0]!=DA_MAGIC || blob[1]!=DA_NVS_VERSION) return out;
  int o = 3;
  for(int r=0;r<blob[2];r++){
    DaRec d; int next = daGetRecord(blob.data(), o, (int)blob.size(), d); if(next < 0) break; o = next;
    bool dup=false; for(auto& e : out) if(e.port==d.port && e.type==d.type) dup=true;
    if(dup || perPort[d.port] >= DA_PER_PORT) continue;
    perPort[d.port]++; d.flags = 0; out.push_back(d);                    // reloaded = not heard
  }
  return out;
}
static void test_da_nvs_blob(){
  printf("WDP-DA: the saved list reloads in order, quiet...\n");
  std::vector<DaRec> L = { daRec(2,"PSI Front","1.4"), daRec(2,"PSI Rear","1.4"), daRec(5,"Stealth","3.0",true,"rev2") };
  std::vector<uint8_t> blob(3); blob[0]=DA_MAGIC; blob[1]=DA_NVS_VERSION; blob[2]=(uint8_t)L.size();
  for(auto r : L){ r.flags = 0; uint8_t rec[DA_REC_MAX]; int n = daPutRecord(rec,0,sizeof(rec),r); blob.insert(blob.end(), rec, rec + n); }
  auto back = daLoad(blob);
  bool ok = back.size()==3 && back[0].type=="PSI Front" && back[1].type=="PSI Rear" && back[2].hw=="rev2";
  for(auto& r : back) if(r.flags) ok = false;
  CHECK(ok, "order and fields survive; every record comes back not-live");
  std::vector<uint8_t> cut(blob.begin(), blob.end() - 3);
  CHECK(daLoad(cut).size()==2, "a truncated blob keeps the records before the damage");
}

int main(){
  printf("== WDP wire-format & election spec test ==\n");
  test_roundtrip();
  test_unknown_and_truncated();
  test_seqhash();
  test_maestro_cfg_supersedes();
  test_pwmtarget_self_only();
  test_solicit();
  test_mac_binding();
  test_cap_election();
  test_autojoin_vetting();
  test_da_single_frame();
  test_da_multi_frame_any_order();
  test_da_lost_frame_keeps_old();
  test_da_empty_and_repeat();
  test_da_hash_tracks_state();
  test_da_malformed();
  test_da_scrub();
  test_da_pool_full();
  test_da_nvs_blob();
  if(g_fail){ printf("\n%d CHECK(s) FAILED\n", g_fail); return 1; }
  printf("\nAll checks passed.\n");
  return 0;
}
