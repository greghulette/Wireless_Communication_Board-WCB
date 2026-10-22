# Channel sync — keeping the whole mesh on one radio channel

**Scope:** the WCB firmware (`Code/WCB/WCB_ChanSync.{h,cpp}`, wire format and pure decisions in
`WCB_ChanSyncWire.h`), the `WCB_Client` library (the same header, byte-identical, in `src/WCB_ChanSyncWire.h`), and
NaviCore, which leads the mesh when it is present.

The ESP32 has one radio, so every board, every `WCB_Client` device, and any access point a board hosts or joins share
one channel ([WIFI_DESIGN.md](WIFI_DESIGN.md) §2). A board on another channel hears nothing and nobody hears it.
Nothing reports that.

Channel sync moves the mesh together and brings stray boards back:

- **MOVE.** The device that leads announces "the mesh moves to channel N" to every device it hears. They switch
  together.
- **Search.** A group's leader that misses a board it knows about visits the other channels, asks who is there, and
  either takes its group there or pulls the other group home.

It is on by default (`?CHSYNC,ON`) and needs ETM and WDP (`chanSyncActive`): every frame rides the ETM struct and
gate, and the CHAN facts that say who leads ride the WDP advert - a board with WDP off would hear none and take itself
for every group's leader. A board with it off, or inactive, behaves exactly as before: it never moves, never searches,
and never answers a search.

---

## 1. Who leads

Every device has a **key**: its id, its **rank**, and whether it is **pinned**. Among the devices that take part (SYNC
on), the best key present leads, and the mesh's channel is the leader's channel.

| Rank | Device |
|---|---|
| 0 | NaviCore (`setChanSyncRank(0)` in its sketch) |
| 1-20 | WCB *n* has rank *n* |
| 100 + id | any other `WCB_Client` device (the library default) |
| 255 | never leads: temporary peers, management relays. They still follow. |

Ordering (`chansync::better`): a device that can lead beats one that cannot; then a **pinned** device beats an unpinned
one; then the lower rank; then the lower id.

**Pinned means associated with an access point** — a WCB whose station is associated, read from the driver
(`wcbWifiJoinAssociated`), never from the saved `?WIFI` mode, which changes at once while the interface changes only at
the next boot. Its channel is the access point's and nothing on the mesh can move it, so the mesh has to go
to it. That is why pinned outranks rank: with NaviCore (rank 0) present and WCB 3 joined to a router on channel 6, the
mesh goes to channel 6 and NaviCore follows. Two pinned devices on two channels cannot both keep their networks; the
lower rank keeps its, and the other drops its association and follows.

**A device that does not take part never leads.** A board with `?CHSYNC,OFF`, or firmware without channel sync (whose
key is derived, §2, with SYNC off), cannot move or search, so a group it led would never look for a stray board and
would refuse every MOVE but its own. It is left out of the leader computation (`look()` in `WCB_ChanSync.cpp`). A
**pinned** one still counts where keeping a network matters: an operator MOVE is refused, and a JOIN board does not
take the mesh away from it (§3, §5).

A device that loses its leader does **not** move. If NaviCore is switched off, WCB 1 leads on the channel everyone is
already on. The mesh only ever moves toward a better device that is on, on another channel.

**Who counts as heard:** the board's members — the WCBQ floor, learned and temporary peers, and the controller peer —
that the ETM sweep holds online. A member that can lead and is offline (55 s of silence, or never heard since boot) is
**missing**; temporary peers and rank-255 devices are never missing.

---

## 2. What each device advertises

WDP TLV **`0x14` CHAN**, in every advert: `[channel][rank][flags][cc0][cc1]`. Flags: `0x01` PINNED, `0x02` SYNC (takes
part). `cc` is the region the device applied at boot (§6). The board places it before its port labels, and the advert
change hash covers it, so a change (associating, moving, `?CHSYNC,OFF`) re-advertises within 500 ms. A value longer
than 5 bytes is accepted (fields may be appended); a shorter one is ignored.

A receiver keeps it in the neighbor table (`WdpNeighbor::hasChan`, `::chan`) and uses it while the advert is fresh
(WDP's 180 s TTL). A neighbor that never sent one (older firmware), or whose advert went stale, gets a derived key: a
WCB's rank is its number, a client's is 100 + id, a temporary peer's 255; unpinned, SYNC off.

`?WDP,DUMP` prints one line per device that sent the TLV, itself first (in the self block, after `[WDPSEQ:...]`):

```
[WDPCH:N=<id>,CH=<channel>,RANK=<rank>,PIN=<0|1>,SYNC=<0|1>,CC=<cc>]
```

`?WDP,<n>` shows the same facts as a `Chan sync` line, or says the device does not advertise them.

---

## 3. Moving: MOVE

Frames are ETM-struct broadcasts on packet types **25 MOVE, 26 PROBE, 27 PROBE_REPLY**, payload in `structCommand`
(layout in `WCB_ChanSyncWire.h`), sent through the WDP envelope (`chanSyncBroadcastFrame` → `wdpBroadcastAs`, so
through `wcbEspNowSend`, CLAUDE.md rule 16). They are not commands: `lastReceivedViaESPNOW` never gates them. Released
firmware drops an ETM packet type it does not know at the fall-through `return` that ends the ETM receive path in
`espNowReceiveCallback` (`WCB.ino`), after only the presence refresh; `WCB_Client`'s packet switch has no `default`.
So these break no older peer.

The receive callback only copies a frame into a 16-slot queue (`chanSyncOnFrame`); `chanSyncTick()` in `loop()`
judges, prints and sends (CLAUDE.md rule 11). Channel-sync frames never refresh the sender's presence: a PROBE or a
pull comes from a board visiting from another channel, and marking it online would clear the very "board missing" its
group's leader searches on.

A MOVE goes out **5 times, 300 ms apart**. Each frame carries the time left until the switch, starting at 1.5 s, so a
device that hears only the last frame still switches with the rest. A follower acts on the first frame of a `moveId`
(per sender) and ignores the others.

A follower obeys a MOVE (`chansync::judgeMove`) unless:

- sync is off on it;
- the channel is outside its region, or 0, or above 13;
- it is already there (no line printed);
- it is **pinned** and the sender does not outrank it (an operator move never takes a pinned board off its network);
- a device it hears **outranks the sender** — then the sender is not its leader and should not be moving anyone. This
  is what stops a confused board from dragging the mesh.

A refusal prints one line naming the reason. A board in the middle of a search holds the latest MOVE it hears at
home and judges it once the radio is home, before acting on what it found - so a MOVE made meanwhile, an operator's
above all, is followed rather than undone by a pull. One whose move already waits on a restart ignores further ones. A
leader sends no MOVE frame while its radio is away (a JOIN full-band scan).

**Applying a move** (`applyMove`), at the switch time:

- A board with an **access point up, or JOIN associated**, saves the channel and restarts through `rebootPending` (the
  deferred restart, CLAUDE.md rule 11), so the interface comes up on the new channel.
- Any other board switches the radio live (`esp_wifi_set_channel`), sets `meshChannel`, and saves the channel. A JOIN
  board that is not associated cancels a connect in progress first. ESP-NOW peers are registered on channel 0 ("the
  current channel"), so nothing needs re-registering. If the driver refuses the channel live, the board saves it and
  restarts onto it.
- A board with a **restart already pending** saves the channel at once and lets that restart take it (waiting would let
  the restart come up on the old channel and lose the move).
- A board that is **busy** — an OTA session (`otaActiveSession`), a config pull job (`configPullJobActive`), or a JOIN
  scan the driver would refuse a switch during — waits, at most `MOVE_DEFER_MAX_MS` (30 s), then moves anyway.

`;`-commands sent across the switch are lost, the same as across a reboot. ETM retries recover ensured commands.

**Operator move:** `?CHSYNC,MOVE,<ch>` on any board sends a MOVE with the OPERATOR flag, which every non-pinned
follower obeys whoever sent it, and the board moves with it. It is refused when the board's sync is off, when ETM or
WDP is off, outside the region, while a search or move is running, while a JOIN full-band scan has the radio, and when
the board is pinned or **hears a pinned device**
(sync on or not): that device would stay, the mesh would go, and searching would bring everyone back.

---

## 4. Searching: PROBE / REPLY

**Who searches.** Only a group's leader: a board that hears nobody who outranks it, and that **misses a board it knows
of** (`chansync::shouldSearch`, §1 for "missing"). Followers never search: their leader does it for the group. A board
alone is its own leader.

**When.** Not before 40 s after boot (presence has to settle; every board solicits its neighbors' adverts just after
its first boot advert, so their CHAN facts are in by then), and only once the condition has held for 10 s. Then seven
attempts, each the gap of 0, 1, 2, 4, 8, 16, 32 minutes after the one before STARTED - an attempt held up (a station
on the access point, a busy board) is not followed at once by the next. After the seventh it stops until the next boot,
`?CHSYNC,SEARCH`, or the condition has stayed clear for 10 minutes (`TRIGGER_RESET_MS`) and comes back; a condition
back sooner carries on its schedule, at the earliest 10 s after it returned, so a member that flaps offline and online
costs no more than one schedule. A board that is simply switched off costs its leader seven short searches, not one an
hour forever. Whether the board can hop is checked when an attempt is due, not as part of the condition, so a station
joining and leaving an access point does not restart the schedule; a due attempt waits until the board can go.

**Who can hop** (`wcbWifiCanHop`). A board with no interface up; a board hosting an access point with **no stations
connected** (IDF: `esp_wifi_set_channel` must not be called with stations attached); a JOIN board only while it is
neither associated, scanning (either scan, §5), connecting (5 s after a `WiFi.begin`), nor moving to its network. A
search also waits (`chanSyncHostBusy`) for no command to have run for 1 s (`CHSYNC_QUIET_MS`), an empty command queue,
no ETM command awaiting an ACK, no OTA session, config pull, pending restart or ETM characterization, and no RC
passthrough in use: no PWM (`;P`), raw Kyber or raw-serial frame sent or heard for 5 s (`CHSYNC_RC_QUIET_MS`) -
those are unacknowledged, and PWM is sent only on a change, so one lost to a hop would leave a servo off where it
should be. When the radio comes home the PWM passthrough sends every input once more anyway
(`pwmResendPassthrough`), as it does after a live move. `?CHSYNC,SEARCH` is refused on a follower, naming the board that
leads: only a group's leader searches.

**How.** For each other channel the region allows: switch, broadcast a PROBE carrying a per-channel nonce, listen
150 ms, move on; then return home. About 1.5 s off the mesh in all. Every device with sync on that hears a PROBE
answers with a REPLY naming the best device it hears, itself included and the prober left out (it can still read
online there from before it left that channel), after `(id % 16) × 3 ms` so a group does not answer in lockstep.
REPLYs are broadcast: the prober is not a registered peer there. The prober takes a REPLY only for the channel and nonce
it is listening for, and holds a responder to its own key too. A REPLY that names the prober itself anyway (a responder
that does not leave it out) counts only for the responder's own key: the prober would otherwise compare itself with itself and neither
go nor pull. No REPLY goes out while the responder's own radio is away.

**While the radio is away** (`chanSyncRadioAway()`: a search hop or pull, or a JOIN full-band scan, §5) the receive
path drops everything but channel-sync frames, and `loop()` holds the heartbeat, the WDP advert, ETM retries and every
command source (the timed groups, the command queue, WebSocket lines, config-pull replies), whose sends would go out
where nobody they are for can hear them. Each catches up on the first pass home: a command typed during a search runs
about 1.5 s late, never on the visited channel. A frame received away is
marked so: only a search's REPLYs are taken from one.

**Then** (`chansync::judgeSearch`), per channel, the best leader any reply named:

- a leader **better than this board** somewhere: announce a MOVE to that channel at home (its group follows), and go;
- otherwise, for every channel with a **worse** leader: visit it and send its group a MOVE home (3 frames, 100 ms
  apart, 600 ms countdown, then 50 ms before leaving the channel) — a *pull*;
- nobody: nothing.

Two leaders searching at once converge: the worse one goes to the better one, and the better one pulls the worse one
home, which is the same channel.

**The dome case.** The dome boards were off when the mesh moved. They wake on the old channel, hear each other, and are
content — except their leader misses the body boards. It searches, finds them, and takes the dome group across.

---

## 5. Joining a network on another channel

A JOIN board with sync on (and ETM on) looks for its network on every channel, not just the mesh's, when it is not
associated (`WCB_WiFi.cpp`, the channel-sync section): a full-band scan right after a mesh-channel scan misses it, then
at 1, 2, 4, 8, 16 and 30 minutes after the network went missing (boot, or the association lost), then every 30 minutes.
Each waits for the same gate as a search (`chanSyncMayLeaveChannel`): not inside the 40 s boot grace - a decision taken
with no presence and no CHAN facts could not see a pinned device that outranks this one - and not while the board is
busy. One that waits is tried at the next mesh-channel miss, 5 s on. A full scan running when `?WIFI` changes the mode,
or when an association lands, is still finished and the radio put back; only a JOIN board still looking acts on it.
A full scan covers every channel the region allows, 120 ms each, with the same SSID filter, and takes the board off the
mesh for about 1.5 s; the radio is put back on the home channel explicitly when it ends, rather than trusting the driver
to return an unassociated station there (not yet observed on the bench either way). The mesh-channel scan every 5 s stays as it was (tracker #103: an unconfined connect scan
swept the band on every attempt). No full scan runs while a search or move has the radio.

When it finds its network on channel *c* that is not the mesh's (`chanSyncJoinFollow`):

- if a pinned device it hears outranks it as it would be once pinned (another board joined to another network), it
  stays and says so once — two networks on two channels cannot both be kept;
- if *c* is outside its region, it stays and says so once (§6);
- otherwise it is about to be the pinned leader: it announces a MOVE to *c* with the PINNED flag (so a better unpinned
  device, NaviCore, follows it), switches with everyone, and then calls `WiFi.begin` with the BSSID the scan found, on
  *c*.

The post-association check that disconnects an access point on the wrong channel stays as the backstop. With channel
sync on it no longer settles the join for good: it starts the full-band schedule over, so a network that moved channel
under an association is found where it went. With sync off it settles, as before.

When its access point changes channel (routers on "auto" do), the board loses the association, finds the network again
on the new channel, and leads the mesh there.

None of this runs without a WebSocket token: JOIN refuses to start at boot when none is set, which settles the join
before any scan ([WIFI_DESIGN.md](WIFI_DESIGN.md) §7).

---

## 6. Region: channels 12 and 13

ESP-IDF's default region is world-safe mode `"01"`: channels 1-11. Channels 12-13 need a region that allows them, set
on **every** device:

```
?REGION,<cc>     cc = 01 (default) or an IDF country code: AT AU BE BG BR CA CH CN CY CZ DE DK EE ES FI FR
                 GB GR HK HR HU IE IN IS IT JP KR LI LT LU LV MT MX NL NO NZ PL PT RO SE SI SK TW US
```

Saved in NVS (`wcb_config/region`), applied at the next boot (`chanSyncApplyRegionAtBoot`) after `WiFi.mode()` and
before the channel is set and before `wcbWifiStart()` brings an access point up: `esp_wifi_set_country_code(cc, false)`.
802.11d off, so the configured region always applies — with it on, a JOIN board would adopt its router's region while
associated and the others could not follow. `"01"` makes no call, so the default is the previous behaviour - unless
the driver already holds another country (`esp_wifi_get_country_code`): the IDF keeps the country in its own NVS when
its storage is FLASH, which other firmware on the chip may have left. Only then is it reset to `"01"`, 802.11d on as
the default has it, with a boot line. This firmware's own call never reaches that flash copy (`WiFi.persistent(false)`
has set `WIFI_STORAGE_RAM` first), so `?REGION,01` really returns a board to the default. The top channel is the driver's (`esp_wifi_get_country`
`schan + nchan - 1`), capped at 13: channel 14 is never used.

**A saved channel the region does not allow** — the region was changed back, or a backup from a board in another
region was restored — runs on channel 1 for that boot, with a loud boot line. The saved value stays, so fixing the
region alone brings it back at the next boot. Channel 1 is the factory default, where older firmware also put an
out-of-range channel.

`?WCBCH` and every MOVE are checked against the region's top channel. `?WCBCH` checks against 13 instead while
`?REGION` has saved a region other than the one running (the next boot applies it and checks there) and says so — which
is why a config chain carries `?REGION` before `?WCBCH`. Legality is the operator's call: 12-13 are legal in Europe and
most of the world; WiFi devices in the US are limited to 1-11.

---

## 7. Commands (WCB)

| Command | Effect |
|---|---|
| `?CHSYNC` | status block (channel and region, this board's key, the leader, members heard and missing, state, the search schedule), and one machine line `[CHSYNC:EN=,CH=,MAXCH=,CC=,RANK=,PIN=,LEAD=,LRANK=,LPIN=,STATE=idle\|search\|move]` |
| `?CHSYNC,ON` / `?CHSYNC,OFF` | take part or not (NVS `wcb_config/chsync`, default ON; live — OFF ends a search or a move not yet applied) |
| `?CHSYNC,SEARCH` | one search, as soon as the command queue is quiet (within 10 s) and if this board can hop; it restarts the attempt schedule. Refused on a follower, naming the board that leads |
| `?CHSYNC,MOVE,<ch>` | operator move of the whole mesh (§3) |
| `?REGION` | `Region: <cc>, channels 1-<n>`, and `Saved for the next boot: <cc>` when one is pending |
| `?REGION,<cc>` | save a region for the next boot |

Config pulls and `?backup` carry `?REGION,<cc>` (the saved one, just before `?WCBCH`) and `?CHSYNC,ON|OFF`, always.

---

## 8. WCB_Client and NaviCore

The library (≥ 1.19.0) implements the same advert, MOVE, PROBE and REPLY handling and the same search. Its host sketch
sets the rank and region and decides how a move is applied:

```cpp
wcb->setChanSyncRank(0);                 // NaviCore; default 100 + id; 255 = never lead
wcb->setRegion("DE");                    // before begin(); default "01"
wcb->onChannelMove([](uint8_t ch) {      // loop context (update())
  saveChannel(ch);                       // persist, so the next boot starts there
  return requestRestart();               // true: the host applies it (a SoftAP moves by restarting)
});                                      // false: the library switches the radio live (no SoftAP only)
```

NaviCore leads at rank 0, saves a move into `wcbNetwork.channel`, and restarts to move its access point. A NaviCore with
a laptop or phone connected to its access point cannot search (it would move the AP under them); it still follows a
MOVE.

---

## 9. Limits

- **Older firmware does not take part.** It ignores MOVEs, does not answer PROBEs and is left behind when the mesh
  moves. Its key is derived (§2), so the Wizard can show who will be stranded. It does mark a visiting prober present
  (its presence refresh runs before its packet-type check), so a board searching past it reads online there for one
  offline window.
- **ETM must be on**, as for WDP.
- **A board that was never told about a better one stays put.** A search looks for missing *known* boards; a brand-new
  board learns the others from their adverts on its own channel.
- **A member that is never coming back costs searches.** A learned peer that was removed from the droid, or a WCBQ floor
  set above the boards that exist, is missing for good: its leader searches seven times after every boot, about 1.5 s
  off the mesh each. `?WDP,FORGET,<id>` / `?WCBQ` remove it.
- **Router channel changes move the droid** each time. A router that hops often makes the mesh hop with it.
- **Same trust as every other mesh packet.** A device that knows the mesh password can send a MOVE. An operator move can
  already be had today with `?WCBCH` and a restart; searching brings stranded boards back.

---

## 10. Pointers into the code

- `Code/WCB/WCB_ChanSyncWire.h` — frames, ordering, `judgeMove`, `shouldSearch`, `judgeSearch`, timings
  (`tests/chansync_wire_test.cpp`; the CHAN TLV decode is also in `tests/wdp_wire_test.cpp`).
- `Code/WCB/WCB_ChanSync.{h,cpp}` — keys (`selfKey`, `keyOf`, `look`), the frame queue, `applyMove`, the search
  (`startSearch`, `tickSearch`, `judge`), the trigger (`evaluateTrigger`), `?CHSYNC`, `?REGION`,
  `chanSyncApplyRegionAtBoot`, `chanSyncJoinFollow`.
- `Code/WCB/WCB.ino` — the receive gates in `espNowReceiveCallback`, `chanSyncBroadcastFrame` / `chanSyncHostBusy`,
  the `loop()` holds while away, `?WCBCH`'s region check, `?REGION` / `?CHSYNC` in `collectConfigCommands`, the boot
  order in `setup()`.
- `Code/WCB/WCB_WiFi.cpp` — the channel-sync section: full-band scans, `csFollow`, `wcbWifiCanHop`,
  `wcbWifiJoinAssociated`; the off-mesh guard and the drift check in `wcbWifiService`.
- `Code/WCB/WCB_WDP.cpp` — TLV 0x14 in `wdpBuildPayload` and `wdpOnAdvertReceived`, `[WDPCH:...]`.
- HIL: `tests/hil/suites/s55_chsync.py` (opt-in `chsync`, [HIL_TESTING.md](HIL_TESTING.md)).

---

## Revision log

| Date | Commit | Change | Why |
|---|---|---|---|
| 2026-10-21 | — | §4: every command source waits while the radio is away (timed groups, the command queue, WebSocket lines, config-pull replies), not only the heartbeat, adverts and ETM retries. | HIL `ota.relay_navicore_brick_guard` (run 20261021-212944): a relayed `?OTA,BEGIN` typed while W1 searched went out on the visited channel and NaviCore never answered. |
| 2026-10-21 | — | Initial design: leader key (pinned, rank, id), CHAN TLV `0x14`, MOVE/PROBE/REPLY on ETM types 25-27, search by group leaders, JOIN follows its network across channels, `?REGION` for 12-13. | Greg: a droid joining a network on another channel should take its mesh with it, and stray boards should find their way back. |
| 2026-10-21 | — | Built in the WCB firmware (`WCB_ChanSync.{h,cpp}`); this page now describes it as built. Settled in the build: a device that does not take part (sync off, older firmware) never leads, but a pinned one still blocks an operator move and a JOIN takeover; frames never refresh presence; while the radio is away the receive path keeps only channel-sync frames and `loop()` holds heartbeats, adverts and ETM retries; can-hop is checked when an attempt is due, not in the trigger; a MOVE that finds a restart pending saves the channel for it instead of waiting; a search also waits for 1 s without a command and no ETM ACK outstanding; a saved channel the region forbids runs on 1 for that boot, NVS kept; `?WCBCH` takes 13 while another region is pending; with sync on the JOIN off-mesh guard no longer settles. | Implementation. |
| 2026-10-21 | — | Review fixes: a JOIN full-band scan is finished, and the radio put back, on every path (a `?WIFI` change mid-scan left the receive path deaf for good); JOIN-follow and full scans wait for the boot grace and for an idle board, RC passthrough included, and boards solicit adverts at boot; the search schedule re-bases on a late attempt and a flapping member keeps its schedule for 10 minutes; the WiFi state channel sync reads is what runs, not the saved mode; a MOVE heard mid-search is judged once home, MOVE frames and REPLYs are held while the radio is away, a responder leaves the prober out of its REPLY and a prober credits the responder for one that names it anyway; `?CHSYNC` says what holds a due search; WDP off makes channel sync inactive; `?CHSYNC,SEARCH` only on a group's leader; PWM passthrough re-sent when the radio returns; at boot with region 01 a country the driver kept from before is reset. | Independent review of the merged firmware. |
