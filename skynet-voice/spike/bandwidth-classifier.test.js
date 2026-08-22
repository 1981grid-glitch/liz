// v4: ask the physical question directly — is there speech content in each
// diagnostic band, measurably above the noise floor? Refuse to classify when
// dynamic range is too small for the answer to mean anything.
const sampleRate = 48000, bins = 2048;
const minDb = -100, maxDb = -30, dbRange = maxDb - minDb;
const unitsPerDb = 255 / dbRange;
const nyquist = sampleRate / 2, hzPerBin = nyquist / bins;

function synth(cutoffHz, floorDb, slopeDbPerHz) {
  const a = new Float32Array(bins);
  for (let b = 0; b < bins; b++) {
    const f = b * hzPerBin;
    let db;
    if (f < 200) db = -70;
    else if (f <= cutoffHz) db = -35 - Math.max(0, 12 * Math.log2(Math.max(f, 500) / 500));
    else db = Math.max(floorDb, -35 - 12*Math.log2(Math.max(cutoffHz,500)/500) - (f - cutoffHz)*slopeDbPerHz);
    a[b] = ((Math.max(minDb, Math.min(maxDb, db)) - minDb) / dbRange) * 255;
  }
  return a;
}

function analyse(maxHold) {
  const toDb = (byte) => minDb + (byte / 255) * dbRange;
  const meanDbIn = (lo, hi) => {                 // mean level across a band, in dB
    let s = 0, n = 0;
    const b0 = Math.max(0, Math.floor(lo / hzPerBin));
    const b1 = Math.min(bins - 1, Math.ceil(Math.min(hi, nyquist) / hzPerBin));
    for (let b = b0; b <= b1; b++) { s += toDb(maxHold[b]); n++; }
    return n ? s / n : minDb;
  };
  // noise floor: median of the top 10% of bins (above any real mic content at 48 kHz)
  const tail = [];
  for (let b = Math.floor(bins * 0.9); b < bins; b++) tail.push(toDb(maxHold[b]));
  tail.sort((a, b) => a - b);
  const floorDb = tail[Math.floor(tail.length / 2)];

  const refDb   = meanDbIn(1000, 3000);   // always present if anyone spoke
  const midDb   = meanDbIn(4500, 6500);   // present for mSBC, absent for CVSD
  const highDb  = meanDbIn(9000, 12000);  // present only for a non-HFP mic
  return {
    floorDb, refDb, midDb, highDb,
    dynamicRange: refDb - floorDb,
    midOverFloor: midDb - floorDb,
    highOverFloor: highDb - floorDb
  };
}

const PRESENT = 6;      // dB above floor to call a band "occupied"
const MIN_DR  = 35;     // dB of usable range needed before a verdict means anything

function classify(m) {
  if (m.dynamicRange < MIN_DR) return 'INCONCLUSIVE (dynamic range ' + m.dynamicRange.toFixed(0) + ' dB)';
  const mid = m.midOverFloor > PRESENT, high = m.highOverFloor > PRESENT;
  if (!mid && !high) return 'NARROWBAND ~8k (CVSD)';
  if (mid && !high)  return 'WIDEBAND ~16k (mSBC)';
  return 'FULL BANDWIDTH (not HFP)';
}

let pass = true;
function run(name, cut, floorDb, slope, want) {
  const m = analyse(synth(cut, floorDb, slope));
  const cls = classify(m);
  const good = cls.startsWith(want);
  if (!good) pass = false;
  console.log(`${good?'PASS':'FAIL'}  ${name.padEnd(24)} DR=${m.dynamicRange.toFixed(0).padStart(3)}dB mid=+${m.midOverFloor.toFixed(0).padStart(2)}dB high=+${m.highOverFloor.toFixed(0).padStart(2)}dB => ${cls}`);
}

console.log('--- primary cases: quiet floor, real codec edge ---');
run('CVSD narrowband',       3600, -96, 0.05, 'NARROWBAND');
run('mSBC wideband',         7600, -96, 0.05, 'WIDEBAND');
run('phone built-in mic',   15000, -96, 0.05, 'FULL');

console.log('\n--- gentler codec rolloff (same verdicts expected) ---');
run('CVSD, gentle rolloff',  3600, -96, 0.02, 'NARROWBAND');
run('mSBC, gentle rolloff',  7600, -96, 0.02, 'WIDEBAND');
run('phone, gentle rolloff',15000, -96, 0.02, 'FULL');

console.log('\n--- degraded capture: must refuse rather than guess ---');
run('mSBC, high floor',      7600, -80, 0.05, 'INCONCLUSIVE');
run('phone mic, high floor',15000, -80, 0.05, 'INCONCLUSIVE');
run('CVSD, high floor',      3600, -80, 0.05, 'INCONCLUSIVE');
process.exit(pass ? 0 : 1);
