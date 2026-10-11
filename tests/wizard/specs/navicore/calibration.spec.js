// The calibration wizard (docs/hil_plan/NAVICORE.md §2, nct.calib.wizard, L1): Config -> Calibrate RC walks the active
// transmitter model's controls (buildCalibrationSteps, config_tool/index.html ~18395), mutes the board with CALIB on,
// reads the live PWM_UPDATE frames (updateCalibrationLive ~18652), captures each control (captureCalibrationValue
// ~18747: a button's matrix band, a switch's or knob's channel by the largest swing, or the channel typed in the box)
// and ends in one Save of the changed branches (finishCalibration ~18883). No board: the emulator streams the frames
// this spec scripts, at 20 Hz, and stores what the Save sends.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

// What the transmitter is doing: the matrix channel's value with the button the firmware decoded from it, and the
// channels (1-based) swinging between two values, one value per frame, as a control moved through its range does.
const live = { matrixVal: 992, btn: 0, swing: {} };
let phase = 0;
function frame(emu) {
  const f = emu.defaultPwmFrame();
  phase ^= 1;
  const channels = Array.from({ length: 24 }, () => 992);
  for (const [ch, [lo, hi]] of Object.entries(live.swing)) channels[ch - 1] = phase ? hi : lo;
  Object.assign(f, { matrixVal: live.matrixVal, btn: live.btn });
  Object.assign(f.sbus, { chCount: 24, frameLen: 36, channels });
  return f;
}

// The wizard's steps as labels: 'info:<label>', 'B<n>', or the switch / knob name; 'done' past the last one.
const label = (s) => (!s ? 'done' : s.type === 'info' ? `info:${s.label}` : s.type === 'button' ? `B${s.btn}` : (s.swName || s.knobName));
const stepLabels = (page) => page.evaluate((src) => calibrationState.steps.map(eval(src)), label.toString());
const current = (page) => page.evaluate((src) => eval(src)(calibrationState.steps[calibrationState.index]), label.toString());
async function nextTo(page, want) {
  for (let i = 0; i < 40; i++) {
    if ((await current(page)) === want) return;
    await page.locator('#btn-calibration-next').click();
  }
  throw new Error(`the wizard never reached step ${want}`);
}
const status = (page) => page.locator('#calibration-summary');
const liveValue = (page) => page.locator('#calibration-live-value');
const capture = (page) => page.locator('#btn-calibration-capture').click();

test.describe('the calibration wizard', () => {
  test.use({ emuOptions: { config: BENCH, monitorHz: 20, pwmFrame: frame } });

  test('nctool.calibration_wizard Calibrate RC mutes the board (CALIB on) and walks the X18\'s 38 steps: a pressed button captures its matrix band and auto-advances, a mismatched one is corrected, a switch or knob takes the channel that swung the most or the one typed in, too little movement is refused, Previous keeps a capture, and Save & Close un-mutes and saves the thresholds, switches and knobs that changed', async ({ page, emu }) => {
    Object.assign(live, { matrixVal: 992, btn: 0, swing: {} });
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.locator('#btn-hwsetup').click();
    await page.locator('#btn-calibrate').click();
    await expect(page.locator('#calibration-modal')).toHaveClass(/open/);
    const calib = await T.waitRequests(emu, 'CALIB');
    expect(calib[0]).toMatchObject({ type: 'CALIB', on: true });
    await expect.poll(() => emu.calib).toBe(true);
    // The X18 with the 3-axis gimbals off: pre-flight, 18 buttons, SA-SJ, the joystick note, 8 knobs.
    expect(await stepLabels(page)).toEqual([
      'info:Pre-flight', ...Array.from({ length: 18 }, (_, i) => `B${i + 1}`),
      'SA', 'SB', 'SC', 'SD', 'SE', 'SF', 'SG', 'SH', 'SI', 'SJ', 'info:Joystick Section',
      'S1', 'S2', 'LS', 'RS', 'J1', 'J2', 'J3', 'J4']);
    await expect(page.locator('#calib-step-counter')).toHaveText('Step 1 of 38');
    await expect(page.locator('#btn-calibration-prev')).toBeDisabled();
    await expect(page.locator('#btn-calibration-capture'), 'an info step has nothing to capture').toBeHidden();
    await page.locator('#btn-calibration-next').click();

    // B1: a press outside the neutral band captures by itself (band = value +-10) and, auto-advance on, moves on.
    expect(await current(page)).toBe('B1');
    await expect(page.locator('#btn-calibration-capture')).toHaveText('Force Capture');
    await expect(liveValue(page)).toHaveText(/Detected: none/);
    live.matrixVal = 1811; live.btn = 1;
    await expect(status(page)).toHaveText(/B1 captured at .*Auto-advancing/);
    live.matrixVal = 992; live.btn = 0;      // released before the advance: B2 would capture the same press otherwise
    await page.clock.fastForward(1000);
    await expect.poll(() => current(page)).toBe('B2');
    expect(await page.evaluate(() => config.thresholds[0])).toEqual(expect.objectContaining({ id: 1, minPwm: 1801, maxPwm: 1821 }));

    // Auto-advance off (remembered): B2 pressed while the firmware's stale bands decoded B3 is captured as B2, noted,
    // and waits for Next; a Force Capture with nothing pressed is refused.
    await page.locator('#calib-auto-advance').uncheck();
    expect(await page.evaluate(() => localStorage.getItem('rcCalibAutoAdvance'))).toBe('0');
    live.matrixVal = 1770; live.btn = 3;
    await expect(status(page)).toHaveText(/B2 captured at .*Click Next → to continue\. \(FW saw .* — corrected\)/);
    live.matrixVal = 992; live.btn = 0;
    await page.clock.fastForward(1000);
    expect(await current(page)).toBe('B2');
    await capture(page);
    await expect(status(page)).toHaveText(/No button pressed/);

    // SA (bench ch 8): nothing moved yet is refused; a swing on ch 8 is detected as the current assignment and kept.
    await nextTo(page, 'SA');
    await expect(page.locator('#btn-calibration-capture')).toHaveText('Capture Channel');
    await expect(page.locator('#calib-manual-channel')).toHaveValue('8');
    await capture(page);
    await expect(status(page)).toHaveText(/Not enough movement yet/);
    live.swing = { 8: [172, 1811] };
    await expect(liveValue(page)).toHaveText(/SA: detected ch8 .*matches current assignment/);
    await capture(page);
    await expect(status(page)).toHaveText(/SA → ch8 {2}\(auto-detected/);
    live.swing = {};
    await page.locator('#btn-calibration-next').click();
    // SB (bench ch 9) swings on ch 20: detection wins over the stale assignment.
    expect(await current(page)).toBe('SB');
    live.swing = { 20: [172, 1811] };
    await expect(liveValue(page)).toHaveText(/SB: detected ch20 .*\(currently ch9\)/);
    await capture(page);
    await expect(status(page)).toHaveText(/SB → ch20/);
    live.swing = {};
    await page.locator('#btn-calibration-next').click();
    // SC: a channel typed in the box wins over what moved.
    expect(await current(page)).toBe('SC');
    live.swing = { 10: [172, 1811] };
    await page.locator('#calib-manual-channel').fill('21');
    await capture(page);
    await expect(status(page)).toHaveText(/SC → ch21 \(manually set\)/);
    live.swing = {};
    // Previous goes back to SB with its capture in place.
    await page.locator('#btn-calibration-prev').click();
    expect(await current(page)).toBe('SB');
    await expect(page.locator('#calib-manual-channel')).toHaveValue('20');
    // The joystick note, then LS: unassigned on the bench (channel 0) shows the model's default and takes ch 22.
    await nextTo(page, 'info:Joystick Section');
    await expect(page.locator('#btn-calibration-capture')).toBeHidden();
    await nextTo(page, 'LS');
    await expect(page.locator('#calib-manual-channel')).toHaveValue('18');
    live.swing = { 22: [200, 1700] };
    await expect(liveValue(page)).toHaveText(/LS: detected ch22 .*\(currently ch18\)/);   // the model's default stands in
    await capture(page);
    await expect(status(page)).toHaveText(/LS → ch22/);
    live.swing = {};

    // The end: Save & Close un-mutes the board and saves exactly the branches the captures touched.
    await nextTo(page, 'done');
    await expect(status(page)).toHaveText('Calibration complete! 6 values captured.');
    await expect(page.locator('#btn-calibration-next')).toHaveText('Save & Close');
    await expect(page.locator('#btn-calibration-capture')).toBeDisabled();
    await page.locator('#btn-calibration-next').click();
    await expect(page.locator('#calibration-modal')).not.toHaveClass(/open/);
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(Object.keys(sent.data).sort()).toEqual(['knobs', 'switches', 'thresholds']);
    expect(sent.data.thresholds[0]).toEqual(expect.objectContaining({ id: 1, minPwm: 1801, maxPwm: 1821 }));
    expect(sent.data.thresholds[1]).toEqual(expect.objectContaining({ id: 2, minPwm: 1760, maxPwm: 1780 }));
    expect(sent.data.thresholds[2]).toEqual(BENCH.thresholds[2]);
    expect([sent.data.switches.SA.channel, sent.data.switches.SB.channel, sent.data.switches.SC.channel, sent.data.switches.SD.channel]).toEqual([8, 20, 21, 11]);
    expect(sent.data.knobs.LS.channel).toBe(22);
    expect(sent.data.knobs.RS.channel).toBe(BENCH.knobs.RS.channel);
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    const board = emu.config.toJSON();
    expect(board.thresholds[0]).toEqual(expect.objectContaining({ minPwm: 1801, maxPwm: 1821 }));
    expect(board.thresholds[1]).toEqual(expect.objectContaining({ minPwm: 1760, maxPwm: 1780 }));
    expect([board.switches.SB.channel, board.switches.SC.channel, board.knobs.LS.channel]).toEqual([20, 21, 22]);
    expect(emu.requests('CALIB').map((m) => m.on)).toEqual([true, false]);
    expect(emu.calib).toBe(false);
    T.expectNoPageErrors(page);
  });
});

test.describe('the calibration wizard through a WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb' } });

  test('nctool.calibration_via_wcb_refused Calibrate RC over the bridge is refused with the USB instructions (the 5 Hz relay cannot detect a flick): the wizard never opens and no CALIB goes out', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page);
    await T.openTool(page);
    await T.connectViaWcb(page);
    await page.locator('#btn-hwsetup').click();
    await page.locator('#btn-calibrate').click();
    await expect.poll(() => dialogs.length).toBe(1);
    expect(dialogs[0]).toMatchObject({ type: 'alert', message: expect.stringContaining('Calibration requires a direct USB connection') });
    expect(dialogs[0].message).toContain('choose "Connect via USB", then Calibrate again');
    await expect(page.locator('#calibration-modal')).not.toHaveClass(/open/);
    expect(emu.rx.filter((r) => /CALIB/.test(r.line))).toEqual([]);
    T.expectNoPageErrors(page);
  });
});
