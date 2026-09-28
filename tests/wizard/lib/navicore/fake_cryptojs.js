// A stand-in for crypto-js 4.2.0 (cdnjs), served by page.route (lib/navicore/firmware.js). flasher.js uses one call,
// CryptoJS.MD5(CryptoJS.enc.Latin1.parse(image)).toString(), as esptool-js's calculateMD5Hash (:404-405). This MD5 is
// a marker, not MD5: "md5/<length>/<FNV-1a>" of the string Latin1.parse was given, which proves the tool hashes
// exactly the bytes it hands writeFlash (lib/navicore/fake_esptool.mjs records both).
(function () {
  function fnv(s) {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i) & 0xff; h = Math.imul(h, 0x01000193) >>> 0; }
    return h >>> 0;
  }
  window.CryptoJS = {
    enc: { Latin1: { parse: (s) => ({ latin1: String(s) }) } },
    MD5: (w) => ({ toString: () => `md5/${w.latin1.length}/${fnv(w.latin1)}` }),
  };
  window.__fakeCryptoJsLoads = (window.__fakeCryptoJsLoads || 0) + 1;
})();
