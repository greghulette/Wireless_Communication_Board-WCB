# Channel sync — keeping the whole mesh on one radio channel

**Scope:** the WCB firmware (`Code/WCB/WCB_ChanSync.{h,cpp}`, wire format in `WCB_ChanSyncWire.h`), the
`WCB_Client` library (the same header, byte-identical, in `src/WCB_ChanSyncWire.h`), and NaviCore, which
leads the mesh when it is present.

The ESP32 has one radio, so every board, every `WCB_Client` device, and any access point a board hosts or
joins share one channel ([WIFI_DESIGN.md](WIFI_DESIGN.md) §2). A board on another channel hears nothing
and nobody hears it. Nothing reports that.

Channel sync moves the mesh together and brings stray boards back:

- **MOVE.** The device that leads announces "the mesh moves to channel N" to every device it hears. They
  switch together.
- **Search.** A group's leader that misses a board it knows about visits the other channels, asks who is
  there, and either takes its group there or pulls the other group home.

It is on by default (`?CHSYNC,ON`). A board with it off behaves exactly as before: it never moves, never
searches, and never answers a search.

---

## 1. Who leads

Every device has a **key**: its id, its **rank**, and whether it is **pinned**. The best key present leads,
and the mesh's channel is the leader's channel.

| Rank | Device |
|---|---|
| 0 | NaviCore (`setChanSyncRank(0)` in its sketch) |
| 1-20 | WCB *n* has rank *n* |
| 100 + id | any other `WCB_Client` device (the library default) |
| 255 | never leads: temporary peers, management relays. They still follow. |

Ordering (`chansync::better`): a device that can lead beats one that cannot; then a **pinned** device beats
an unpinned one; then the lower rank; then the lower id.

**Pinned means associated with an access point** — a WCB in `?WIFI,JOIN` that is connected. Its channel
is the access point's and nothing on the mesh can move it, so the mesh has to go to it. That is why pinned
outranks rank: with NaviCore (rank 0) present and WCB 3 joined to a router on channel 6, the mesh goes to
channel 6 and NaviCore follows. Two pinned devices on two channels cannot both keep their networks; the
lower rank keeps its, and the other drops its association and follows.

A device that loses its leader does **not** move. If NaviCore is switched off, WCB 1 leads on the channel
everyone is already on. The mesh only ever moves toward a better device that is on, on another channel.

---

## 2. What each device advertises

WDP TLV **`0x14` CHAN**, in every advert: `[channel][rank][flags][cc0][cc1]`. Flags: `0x01` PINNED, `0x02`
SYNC (takes part). `cc` is the region (§6). The advert hash covers it, so a change (associating, moving)
re-advertises at once.

A receiver keeps it in the neighbor table. A neighbor that never sent one (older firmware) gets a derived
key: a WCB's rank is its number, a client's is 100 + id, unpinned, SYNC off.

`?WDP,DUMP` prints one line per device that sent the TLV, itself first:

```
[WDPCH:N=<id>,CH=<channel>,RANK=<rank>,PIN=<0|1>,SYNC=<0|1>,CC=<cc>]
```

---

## 3. Moving: MOVE

Frames are ETM-struct broadcasts on packet types **25 MOVE, 26 PROBE, 27 PROBE_REPLY**, payload in
`structCommand` (layout in `WCB_ChanSyncWire.h`). Released firmware and `WCB_Client` drop an ETM packet
type they do not know, so these break no older peer. Like WDP they ride the ETM gate, so **they need ETM
on** (CLAUDE.md rule 4).

A MOVE goes out **5 times, 300 ms apart**. Each frame carries the time left until the switch, starting at
1.5 s, so a device that hears only the last frame still switches with the rest. A follower acts on the
first frame of a `moveId` and ignores the others.

A follower obeys a MOVE (`chansync::judgeMove`) unless:

- sync is off on it;
- the channel is outside its region, or 0, or above 13;
- it is already there;
- it is **pinned** and the sender does not outrank it (an operator move never takes a pinned board off its
  network);
- a device it hears **outranks the sender** — then the sender is not its leader and should not be moving
  anyone. This is what stops a confused board from dragging the mesh.

**Applying a move.** A board with no WiFi interface up switches the radio live (`esp_wifi_set_channel`)
and saves the channel. ESP-NOW peers are registered on channel 0 ("the current channel"), so nothing else
needs re-registering. A board that hosts an access point or joins one saves the channel and restarts
through `rebootPending`, so the interface comes up on the new channel. A board that is busy — an OTA
session, a config pull, a restart already pending — applies the move when it is free, at most 30 s late.

`;`-commands sent across the switch are lost, the same as across a reboot. ETM retries recover ensured
commands.

**Operator move:** `?CHSYNC,MOVE,<ch>` on any board sends a MOVE with the OPERATOR flag, which every
non-pinned follower obeys whoever sent it. The board refuses to send it when it hears a pinned device:
that device would stay, the mesh would go, and searching would bring everyone back.

---

## 4. Searching: PROBE / REPLY

**Who searches.** Only a group's leader: a board that hears nobody who outranks it, and that **misses a
board it knows of** (`chansync::shouldSearch`). Known means a member of its peer set (the WCBQ floor,
learned peers, the controller peer); temporary peers and rank-255 devices are not missed. Missing means
offline by the ETM sweep (55 s of silence). Followers never search: their leader does it for the group.
A board alone is its own leader.

**When.** Not before 40 s after boot (presence has to settle), and only once the condition has held for
10 s. Then attempts at 0, 1, 2, 4, 8, 16 and 32 minutes: seven over about an hour. After that it stops
until the next boot, `?CHSYNC,SEARCH`, or the condition clears and comes back. A board that is simply
switched off costs its leader seven short searches, not one an hour forever.

**Who can hop.** A board with no interface up; a board hosting an access point with **no stations
connected** (IDF: `esp_wifi_set_channel` must not be called with stations attached); a JOIN board only
while it is neither scanning, connecting nor associated. A search also waits for the command queue to be
quiet and no OTA or config pull to be running.

**How.** For each other channel the region allows: switch, broadcast a PROBE, listen 150 ms, move on; then
return home. About 1.5 s off the mesh in all. Every device with sync on that hears a PROBE answers with a
REPLY naming the best device it hears, itself included, after `(id % 16) × 3 ms` so a group does not
answer in lockstep. REPLYs are broadcast: the prober is not a registered peer there.

**Then** (`chansync::judgeSearch`), per channel, the best leader any reply named:

- a leader **better than this board** somewhere: announce a MOVE to that channel at home (its group
  follows), and go;
- otherwise, for every channel with a **worse** leader: visit it and send its group a MOVE home (3
  frames, 100 ms apart, 600 ms countdown) — a *pull*;
- nobody: nothing.

Two leaders searching at once converge: the worse one goes to the better one, and the better one pulls
the worse one home, which is the same channel.

**The dome case.** The dome boards were off when the mesh moved. They wake on the old channel, hear each
other, and are content — except their leader misses the body boards. It searches, finds them, and takes
the dome group across.

---

## 5. Joining a network on another channel

A JOIN board with sync on looks for its network on every channel, not just the mesh's, when it is not
associated: a full-band scan at boot when the mesh-channel scan does not find it, then at 1, 2, 4, 8, 16
and 30 minutes, then every 30 minutes. Each full scan takes the board off the mesh for about 1.5 s; the
mesh-channel scan every 5 s stays as it was (tracker #103: an unconfined connect scan swept the band on
every attempt).

When it finds its network on channel *c* that is not the mesh's:

- if a pinned device it hears outranks it (another board joined to another network), it stays and says so
  once — two networks on two channels cannot both be kept;
- if *c* is outside its region, it stays and says so (§6);
- otherwise it is about to be the pinned leader: it announces a MOVE to *c*, switches with everyone, and
  associates there.

The post-association check that disconnects an access point on the wrong channel stays as the backstop.
JOIN also needs a WebSocket token ([WIFI_DESIGN.md](WIFI_DESIGN.md) §7).

When its access point changes channel (routers on "auto" do), the board loses the association, finds the
network again on the new channel, and leads the mesh there.

---

## 6. Region: channels 12 and 13

ESP-IDF's default region is world-safe mode `"01"`: channels 1-11. Channels 12-13 need a region that
allows them, set on **every** device:

```
?REGION,<cc>     cc = 01 (default) or an IDF country code: AT AU BE BG BR CA CH CN CY CZ DE DK EE ES FI FR
                 GB GR HK HR HU IE IN IS IT JP KR LI LT LU LV MT MX NL NO NZ PL PT RO SE SI SK TW US
```

Saved in NVS, applied at the next boot before the channel is set (`esp_wifi_set_country_code(cc, false)`:
802.11d off, so the configured region always applies — with it on, a JOIN board would adopt its router's
region while associated and the others could not follow). `"01"` makes no call at all, so the default is
byte-for-byte the previous behaviour. Channel 14 is never used.

`?WCBCH` and every MOVE are checked against the region's top channel. Legality is the operator's call:
12-13 are legal in Europe and most of the world; WiFi devices in the US are limited to 1-11.

---

## 7. Commands (WCB)

| Command | Effect |
|---|---|
| `?CHSYNC` | status, and one machine line `[CHSYNC:EN=,CH=,MAXCH=,CC=,RANK=,PIN=,LEAD=,LRANK=,LPIN=,STATE=idle\|search\|move]` |
| `?CHSYNC,ON` / `?CHSYNC,OFF` | take part or not (NVS `wcb_config/chsync`, default ON; live) |
| `?CHSYNC,SEARCH` | one search now, if this board can hop |
| `?CHSYNC,MOVE,<ch>` | operator move of the whole mesh (§3) |
| `?REGION[,<cc>]` | show or set the region (NVS `wcb_config/region`; next boot) |

Config pulls and `?backup` carry `?CHSYNC,ON|OFF` and `?REGION,<cc>`.

---

## 8. WCB_Client and NaviCore

The library (≥ 1.19.0) implements the same advert, MOVE, PROBE and REPLY handling and the same search.
Its host sketch sets the rank and region and decides how a move is applied:

```cpp
wcb->setChanSyncRank(0);                 // NaviCore; default 100 + id; 255 = never lead
wcb->setRegion("DE");                    // before begin(); default "01"
wcb->onChannelMove([](uint8_t ch) {      // loop context (update())
  saveChannel(ch);                       // persist, so the next boot starts there
  return requestRestart();               // true: the host applies it (a SoftAP moves by restarting)
});                                      // false: the library switches the radio live (no SoftAP only)
```

NaviCore leads at rank 0, saves a move into `wcbNetwork.channel`, and restarts to move its access point.
A NaviCore with a laptop or phone connected to its access point cannot search (it would move the AP under
them); it still follows a MOVE.

---

## 9. Limits

- **Older firmware does not take part.** It ignores MOVEs, does not answer PROBEs and is left behind when
  the mesh moves. Its key is derived (§2), so the Wizard can show who will be stranded.
- **ETM must be on**, as for WDP.
- **A board that was never told about a better one stays put.** A search looks for missing *known* boards;
  a brand-new board learns the others from their adverts on its own channel.
- **Router channel changes move the droid** each time. A router that hops often makes the mesh hop with it.
- **Same trust as every other mesh packet.** A device that knows the mesh password can send a MOVE. An
  operator move can already be had today with `?WCBCH` and a restart; searching brings stranded boards back.

---

## Revision log

| Date | Commit | Change | Why |
|---|---|---|---|
| 2026-10-21 | — | Initial design: leader key (pinned, rank, id), CHAN TLV `0x14`, MOVE/PROBE/REPLY on ETM types 25-27, search by group leaders, JOIN follows its network across channels, `?REGION` for 12-13. | Greg: a droid joining a network on another channel should take its mesh with it, and stray boards should find their way back. |
