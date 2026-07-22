// ============================================================================
// WDP wire-format & election SPEC TEST  (host-side, no Arduino)
//
// WHAT THIS IS
//   A self-contained regression guard for the parts of the Wireless Discovery
//   Protocol that are pure logic: the TLV encode/decode, the single-owner
//   capability election, the 2-advert auto-join vetting, the sender-MAC binding
//   gate, and the SOLICIT (?WDP,POLL) short-circuit. Runs anywhere a C++11
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

static const uint16_t CAP_HCR = 0x0001, CAP_MP3 = 0x0002, CAP_WLED = 0x0004;

// WDP_BAUD_TABLE — order is the ON-WIRE code; never reorder (WCB_WDP.cpp:113).
static const uint32_t BAUD_TABLE[] = {0,110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000};
static const uint8_t  BAUD_N = sizeof(BAUD_TABLE)/sizeof(BAUD_TABLE[0]);
static uint8_t  baudToCode(uint32_t b){ for(uint8_t i=0;i<BAUD_N;i++) if(BAUD_TABLE[i]==b) return i; return 0xFF; }
static uint32_t codeToBaud(uint8_t c){ return c<BAUD_N ? BAUD_TABLE[c] : 0; }

// ---- encoder: putTLV, mirrors WCB_WDP.cpp:200 ------------------------------
static int putTLV(uint8_t*buf,int o,int max,uint8_t type,const uint8_t*val,int len){
  if(len<0) len=0; if(len>255) len=255;
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
  { int s=2; while(s+2<=200){ uint8_t ty=cmd[s]; if(ty==T_END) break; int ln=cmd[s+1];
      if(s+2+ln>200) break; if(ty==T_SOLICIT) return DK_SOLICIT; s+=2+ln; } }

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
            if(tgt!=(uint8_t)selfWCB) continue; if(prt<1||prt>5) continue;
            bool dup=false; for(int j=0;j<nb.pwmSelfCount;j++) if(nb.pwmSelfPorts[j]==prt) dup=true;
            if(!dup && nb.pwmSelfCount<5) nb.pwmSelfPorts[nb.pwmSelfCount++]=prt; } break; }
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

int main(){
  printf("== WDP wire-format & election spec test ==\n");
  test_roundtrip();
  test_unknown_and_truncated();
  test_maestro_cfg_supersedes();
  test_pwmtarget_self_only();
  test_solicit();
  test_mac_binding();
  test_cap_election();
  test_autojoin_vetting();
  if(g_fail){ printf("\n%d CHECK(s) FAILED\n", g_fail); return 1; }
  printf("\nAll checks passed.\n");
  return 0;
}
