# fenykl.dsp

Encoder, decoder, assembler (an embedded DSL) and listings for the E-M10 Mark II DSP: the engine
at 0x9D000000 that runs 320-bit horizontal microcode words from DRAM. Stdlib only. Stock programs
are read from the user's own firmware image at runtime; no stock microcode is part of this code.

## Layout

| module | contents |
|---|---|
| `fields.py` | the field table, the single source of truth: bits, kind, unit, idle value, confidence, fact; the op meanings `OPS`, the target `TARGET`, the delay slots `DELAY_SLOTS` |
| `units.py` | the units (a strobe and the fields that change under it) and their slot classes, derived from the table |
| `word.py` | `Word`: an immutable 320-bit value with views by field (`word["xreg"]`, `changed()`), by unit (`slots()`) and as a control word (`control`, `end`, `target`); `IDLE` |
| `program.py` | `Program`, the item layout, extraction of the five stock programs, `assemble()` with labels, `call_return_errors()` |
| `disasm.py` | listings and DSL source of a program |
| `memcpy.py` | `Copy16Model`: the model of the stock copy (item 16), built from the firmware's program: what each word does or does without, the transfer channels and roles, inserted words, the resolution of a job into one load and one store and their application |
| `state.py` | tri-state bytes with provenance and taint; the spaces (local memory, buffer, DRAM view); the register files; the register facts and boot parameters |
| `semantics.py` | word classes, parts, roles and channels, the hidden sequencer, `UnknownSemantics`; the op slot's semantics `OP_SEMANTICS` and branch hypotheses (`Given`, `RaTest`) |
| `emu.py` | `Boot`, `Job`, `Executor`: runs a job word by word and has its model apply the trace, or steps a job through its control flow (`Flow`, `Step`) |
| `resample.py` | `Resample13Model`: the model of the stock resampler (item 13), built from the firmware's program: its arithmetic measured from outside, the column blocks and their held line pairs; `model.job` (the stock words with own taps) and `predict` |
| `resample_edits.py` | the edits of program 13 measured on the camera: edit kinds, job classes and per plane what each does |
| `stream.py` | `Engine`: the stream engine's channel registers, loads and stores as X1's transfers program them |
| `machine.py` | `Machine`, the datapath executor of own programs: the register files G, A, B, R, F0..F3, the lane files L0..L3, U and O, the lane memory, the ALU bus, X0, X1, the S heads at their lags and M, run word by word as `Executor.step`'s datapath |
| `exec.py` | the field-level executor of a window of stock words: symbolic values, the machine state (operand and coefficient registers, the load latch, accumulators), the step loop with its latency queue, the coverage rule per (word, field), scoped confidence, the def-use rule and the output stages |
| `resample_window.py` | program 13's loop-head window on the executor: its anchors, semantics and measured cells, the composition cap; the model takes its b = 1 bytes from it |
| `taps.py` | `Taps`: the resampler's 16 phases of 4 s16 taps, checked, packed as the 32 tap words, from floats with an explicit rounding |
| `commands.py` | the CPU setups of the commands as jobs (cmd 0xF, cmd 0xC) |
| `cli.py` | `fenykl dsp disasm ITEM`, `fenykl dsp source ITEM`, `fenykl dsp fields`, `fenykl dsp flow` |

Bit k of a word is bit (k % 8) of byte k // 8 of its 40 bytes. A word decodes from any 320-bit
value, so bytes -> Word -> bytes is exact whatever the table says; the tests check that the table
covers every bit once and that every stock word rebuilds from its field values alone.

The DSL builds a word from slot objects; unset fields keep the idle word's value, which is itself
composed from the table's idle values:

```python
from fenykl.dsp.units import X1, OpSlot
from fenykl.dsp.word import Word

Word(x1=X1(xreg=3, x1op=0x1C), op_slot=OpSlot(f123_127=2))
```

### Units

Every field belongs to one unit. A unit is a strobe (a C2 head or control field that leaves idle
on its own) with the fields whose values change in the words where it is active: over every word
of the programs across later firmware versions of the same family, each held grid or register
field changes almost only under one strobe [verified in the bytes for the counts; the reading as
execution units inferred]. Listings group a word's fields by unit, strobes first.

| unit | strobe | fields | reading | confidence |
|---|---|---|---|---|
| `op_slot` | op f313_319 | imm, ra, rb (f128_132), f120_122, f229 | scalar and control ops | verified |
| `s0` | f278_284 | grid A 37, 41, 72, 76; f23_30 | compute slot: add, compare, neighbour lanes on L0 | inferred |
| `s1` | f285_291 | grid A 45, 49, 80, 84; f0 | compute slot: logic, shifts, min / max on L1 | inferred |
| `s2` | f292_295 | grid A 53, 57, 88 | compute slot: multiply on L2 | verified (every head's row and lag) |
| `s3` | f296_301 | grid A 31, 61, 65, 92, 96 | compute slot: moves, insert, extract, reductions on L3 | inferred |
| `x0` | f302_307 | f1, f2_6, f69_71, f100_104, f105_109 | an ALU on the register file A, and the lane memory's loads and stores (f69_71 the store's register of O) | verified |
| `x1` | f308_312 | f7_8, f9_14, f110_114, xreg (f115_119) | X0's copy on the register file B, and the register transfers | verified |
| `pb` | 17 enables in 232..264 | one grid B nibble each (`fields.PORTS`; f234_235 for f145_146 a hypothesis) | ports; f255..f264's write the scalar files F0..F3, f236..f251's the lane files L0..L3, f252_254's the store file O, f232_233's the third lane file U (verified) | verified (the pairing) |
| `pr` | 5 enables in 265..273 | one field of 206..228 each | register write ports of A, B, R and G, the enable value the source (verified) | verified |
| `m` | f276 (active low) | f274_275 | f276 = 0 puts an S head's scalar result on X0's result bus (verified); f274_275 held: the unit whose result M puts out, 0 S0, 1 S1, 2 S2, 3 S3, among results of one word and for a held result (verified) | verified |
| `q` | f35_36, f145_146 | - | a section mode of the grid, never set on the E-M10 II; set in own programs, it changed nothing (measured, 24 cells) | hypothesis |
| `const` | - | f133_140, f230_231, f277 | constant bits | verified |

S0..S3, PB and M (the compute group) act mostly in words with an idle op, X0, X1 and PR (the
transfer group) with the data ops, early in a routine. Grid B follows grid A at +110 in which
words are active, but not in its values or control: not a mirror bank. The 2-bit PB enables,
f252_254 / f255, f256_257 / f258 and f265_266 / f267_268 are cut where the two bits across a
boundary share little information and each side owns another field (between 241 and 242 mainly
the ownership); f274_275 / f276 by their values. The S0, S1 and S3 heads merge a low part that is
never active without the high part, which is active alone in 5, 5 and 26 % of its words; f232_233
merges a bit that is never clear without the other.

On the camera, ownership predicts use in one direction (`fields.UNIT_FACTS`, program 13 words
433 and 439): idling the nibbles whose owner is active in the word gives the effect of idling the
whole grid, byte for byte, and idling the held nibbles of idle owners has none (32 nibbles, in
three sets, at their idle values). An active owner is not sufficient: port f202_205 and X0's
f69_71 are inert there (X0's head at 439 is no store head).

## Fields

Each field has a permanent positional id from its bits (`f115_119`, `f229`)
and optionally a semantic name (`xreg`). Ids never change for the same bits; names are renamed
freely, without aliases. Slots, `Word[...]` and `Word.from_fields` accept either.

To add or refine a field, edit `FIELDS`: split or merge entries so that the bits stay covered
exactly once (the import raises otherwise), and give the new entries their idle value, kind,
confidence and a one-line fact. An id names both ends of the field, so it changes exactly when the
bits change: DSL source naming the old id fails loudly. Moving a field to another unit is an edit
of its `unit` (and of `fields.HEADS` / `PORTS` where it is a strobe).

Kinds: `reg` (register specifier r0..r31), `opcode`, `route` (source or path select), `flag`
(one control bit), `const` (the same value in every stock word), `imm` (an immediate operand),
`raw` (not decoded).

### The op and control words

Bits 313..319 are one 7-bit op field, `op`, in every word, and f15_22 (`imm`) is that op's 8-bit
immediate, 0 whenever the op is idle (both verified in the bytes of the five stock programs and
across later firmware versions of the same family). `OPS` in the table gives each value its name,
confidence and fact; `fields.op(value)` is total:

| op | name | meaning | confidence |
|---|---|---|---|
| 0 | `loop` | loop end: computes ra - 1 for a port to write back, back to T iff ra > 0 signed | verified |
| 1 | `brf` | branch iff ra == 0, forward, the guard of a loop | verified |
| 2 | `br` | branch iff ra != 0, either way | verified |
| 3 | `call` | a jump to T that writes no link; a call is an op 3 whose delay slots hold 0x0b #(i + 5) on r1 | verified |
| 4 | `ret` | a jump to the word G[f128_132] names, r1 by the stock convention | verified |
| 7 | `halt` | ends the job, at any word and call depth (one camera run inside the copy's routine); the next three words fetched behind it still run and the job ends [verified] (data words; a jump due among them redirects them, verified on op 4 in slots 2 and 3, fitted on op 3; a halt first or an op 3 / op 4 second among them is not taken; the emulator refuses other control words among them and unmeasured jump shapes); word 5 of every stock program, where the call at word 0 returns | inferred |
| 8..73 | `opNN` | a data op (NN in hex); what the measured ones compute is `semantics.OP_SEMANTICS`, each at its own tier (listings mark them by it), the rest open | per op |
| 127 | `idle` | no op | inferred |

5, 6, 74..126 and the data op values 0x22, 0x26, 0x27, 0x2d, 0x39, 0x3b and 0x45 never occur.
Ops 0..4 are the control ops (`CONTROL_OPS`, `word.control`), and
f229 (`ctl_n`) is clear exactly in them. Ops 0..3 name a target T = bits 15..22 | bits 128..132 <<
8, an absolute word index into the program (`word.target`, None for other words): T follows its
target word wherever code moved across later firmware versions of the same family, and lies inside
its program in every op 0..3 word (verified in the bytes). In those words `imm` is T's low byte and
f128_132 holds T's high bits, not a register. The condition of ops 0..2 is the register ra
(f123_127), no field of its own: op 1 branches iff ra == 0, op 2 iff ra != 0, op 0 iff ra > 0
signed, computing ra - 1, which an enabled PR port writes back (the stock loops enable
f272_273 = 1 on ra): `do { body } while (ra-- > 0)`. Neither branch produces a result.

Facts tagged "by exact readout" were measured on the camera with own programs: every result was
stored into DSP local memory and read back as an exact 32-bit word. Values are plain 32-bit
integers that wrap. The scalar unit has four register files:

| file | read by | written by (port, register) |
|---|---|---|
| G | ra, rb | f272_273 on f224_228: 0 X1, 1 the ALU bus, 2 X0; read by the next word |
| R (8 entries) | family A stores (datum R[f120_122]), 0x46 / 0x47 | f270_271 on f221_223: 0 X1, 1 the ALU bus, 2 X0; read by a store in the very next word |
| A | X0 (f100_104, f105_109) | f265_266 on f206_210: 0 X0, 1 X1, 2 the ALU bus; X0 reads it from the next word on |
| B | X1 (f110_114, xreg f115_119) | f267_268 on f211_215: 0 X1, 1 the ALU bus, 2 nothing; f269 = 0 on f216_220: X0 |

Each port's enable value selects its source (`fields.PR_SOURCES`, verified by exact readout); X0
and X1 are their heads' results in the port's own word. A data op writes no register by itself:
its result goes on the ALU bus, which also carries a load's datum three words after the load and
a multiply's product one word after the multiply. The emulator models G (`Boot.slot`) and B
(`Boot.regs`).

The compute side has four more files, F0..F3, of 32-bit scalar registers numbered by a nibble
(`fields.SCALAR_FILES`; verified by exact readout), written by grid B ports and read by S heads
from the next word on:

| file | read by | written by (port, register) |
|---|---|---|
| F0 | S0 head 0x52, F0[f76_79]; S0's lane scalar and select mask s = F0[f72_75]; S0's carry-in mask F0[f76_79] | f255 on f182_185: 0 X0; f256_257 on f186_189: 0 X1, 1 the ALU bus, 2 nothing |
| F1 | S1 head 0x54 and S1's lane scalar t, F1[f84_87]; S1's carry-in mask F1[f80_83] | f258 on f190_193: 0 X0; f259_260 on f194_197: 0 X1, 1 the ALU bus, 2 nothing |
| F2 | S2 head 0xe and S2's lane scalar t, F2[f88_91] | f261_262 on f198_201: 0 X0, 1 X1, 2 the ALU bus |
| F3 | S3 head 0x24, F3[f96_99]; S3's lane index F3[f92_95] and value F3[f96_99] | f263_264 on f202_205: 0 X0, 1 X1, 2 the ALU bus |

The sources per enable value (`fields.PB_SOURCES`) and S3 0x24 = F3[f96_99] are verified on the
camera, every cell pre-registered. Program 13's tap loads reach F2 this way, through f261_262 = 2 three words after
each load, on the registers its S2 words read. The M strobe f276 = 0 puts an S head's scalar
result on X0's result bus, where f272_273 = 2 takes it in the head's own word [verified];
f274_275 names the unit whose result M puts out, 0 S0, 1 S1, 2 S2, 3 S3: among the results of
four heads in one word and for a held result, the other values leaving the sentinel [verified,
pre-registered]; a single head's result in its own word passes under every value [measured]. Under M, S0 0x52, S1 0x54,
S2 0xc and 0xe and S3 0x0..0x5, 0xe, 0xf, 0x16, 0x17 and 0x24 have a scalar result
(`fields.S_RESULTS`; of S0's 0x40..0x5f only 0x52 read): S3 0xe / 0xf extract lane f92_95 + 16 x
(f31_34 bit 2) of L3[f65_68], 0x16 / 0x17 lane F3[f92_95] & 31, zero- / sign-extended
[verified, pre-registered]; S3 0x0..0x5 reduce the 32 lanes of L3[f65_68] to their unsigned maximum, unsigned
minimum, zero-extended sum, signed maximum, signed minimum and sign-extended sum, a result
result on M's bus in the third word after the head and in no other [verified]; S2 0xc reads lane
(f53_56 & 3) x 8 + (f88_91 >> 1) of L2[f57_60] [verified, pre-registered]; S0's and S1's compare heads give a
mask one word after the head (below).

#### Lanes

Each S unit has a lane file of 16 registers of 32 lanes of 16 bits (`fields.LANE_FILES`). The
unit's lane heads read a and b from it through its two lane nibbles, and the two grid B ports
110 bits above those nibbles write it; the enable of a port names the source it takes
(`fields.LANE_SOURCES`; [verified] unless tagged):

| file | unit | a, b | first port (enable): 0, 1, 2 | second port (enable): 0, 1, 2 |
|---|---|---|---|---|
| L0 | S0 | f37_40, f41_44 | f147_150 (f236_237): a load, S2's high half, S0 | f151_154 (f238_239): S3, S2's low half, S1 |
| L1 | S1 | f45_48, f49_52 | f155_158 (f240_241): a load, S2's high half, S0 | f159_162 (f242_243): S3, S2's low half, S1 |
| L2 | S2 | f53_56, f57_60 | f163_166 (f244_245): a load, S0, nothing seen | f167_170 (f246_247): S3, S2's low half, S1 |
| L3 | S3 | f61_64, f65_68 | f171_174 (f248_249): a load, S0, nothing seen | f175_178 (f250_251): S3, S2's low half, S1 |

The operand nibbles were pre-registered at several registers each, 1, 2, 5, 6, 9 and 12 among
them [verified]. Enable 0 of a first port takes the vector an X0 load head asked for exactly
four words earlier (the lane memory, below). A result is on its unit's bus for exactly one word,
the head's own or the next by head, and a port at the other lag writes nothing (`fields.LANE_LAG`,
[verified, pre-registered] for 144 heads, every entry of `fields.LANE_LAG` but the four named
below, the port in one word only: in its own word S0's selects,
S1's logic heads, S2's passes, sign masks and lane number and S3's moves, inserts, broadcast and
lane shifts; one word later
every other S0 and S1 head timed, S2's multiplies and S3's shuffles 0xa..0xd); no head class
predicts it across units. S2 0x7 comes in the head's word [verified, pre-registered], one word
later nothing; S3 0x7 / 0x8 (block selects of b by a's first lanes) one word later and S1
0x60..0x7e in the head's word, S0's one word later (their unit's latched operand) [verified,
pre-registered]; S3 0x6 gives 0 one word later, its lag otherwise open [hypothesis]. A register a
port writes in one word is read by a head in the next [verified, pre-registered: L3 after S3's
and after S2's result, L2 after a load, L0 after S0's and L1 after S1's]; a head in the port's
own word reads the old content, in every lane file and in U [verified, pre-registered].
S3's lane shift 0x1b moves b by n = f92_95 as a signed nibble: down by 1..4 at 15..12 [verified,
pre-registered on all 32 lanes], up by 1..4 [verified, pre-registered on lanes 0..4, 15 and 24..31; up by 2 on
all 32 lanes], not at all at 5..11 [verified, pre-registered at each count]; 0x1c and 0x22 move the lanes as 0x1b at 1..5, 8 and
11..15 [verified, pre-registered; 0x22 at 2 measured only: inferred], 6, 7, 9 and 10 were not run for these two;
`semantics.lane_result` predicts them at the counts run. The lanes moved in come from a third kind of file, U, a register of it named by f31_34
[verified, pre-registered: all sixteen registers read back by number]: its
lanes 0..3 are four carry lanes for either direction [verified, pre-registered, 0x1b]. Its port is f141_144 under
f232_233 = 1 beside an S3 head, 0 and 2 writing nothing [verified, pre-registered: U[3] and U[9]
written in one job and read back by number, U[5] kept under 0 and 2], and the head decides what U
takes: after 0x1c, 0x1f and 0x21 U's lanes 0..3 hold b's lanes 0..3, after 0x1b, 0x1e and 0x20
b's lanes 28..31, after 0x22 nothing [verified, pre-registered; U's other lanes not read]
(`fields.LANE_THIRD_FILE`). That is all that separates the three shifts: 0x1b also saves the lanes
the next vector's shift down needs, 0x1c those the previous vector's shift up needs, 0x22 nothing
[inferred]; the stock programs set U's port beside every 0x1b and 0x1c and beside no 0x22.

The lane functions by head value are `fields.LANE_FUNCTIONS`, each row tagged: verified where
its expectation was registered before the run and met on all lanes of two constructed 32-lane
vectors of edge and random values, or registered again on new operands after a fit (168 heads
with a lane result: S0 59, S1 78, S2 13, S3 18; S0's select heads under masks; 34 more without
one, a sentinel kept at both lags), inferred where it was fitted; every row with a verified lag was registered again on
new operands at that lag and met. In short, S0 adds, subtracts, compares and takes absolute differences, with
neighbour-lane forms (a[k+1]); S1 has logic, shifts by its immediate or t, saturating left
shifts, double-width shifts of the 32-bit a : b and min / max / add with a scalar; S2
multiplies (a x b, b x t and b x an immediate, low 16 bits), passes a or b by bit 1 of f88_91
(0x7) and gives sign masks and the lane number; S3 moves, interleaves, splits odd / even lanes, inserts a lane, broadcasts F3[f96_99]
or a 14-bit immediate and gives the lane number. Halvings and rounded shifts round ties to even
(met at counts 1..3; at count 10 no operand lane was a tie).

S0 0x8..0xd are clamps, clamp(b, lo, hi) = min_s(max_s(b, lo), hi): 0x8 to 0..imm16 (with
imm16 = 0xff the clamp to a byte, also a ReLU), 0x9 to 0..F0[f72_75], 0xb to ~imm16..imm16 and
0xc to ~s..s [verified, pre-registered, non-negative bounds]; 0xa to 0..a and 0xd to ~a..a per lane, with
bounds of both signs by a wrapped rule: for b >= 0, b where the 16-bit b - a has its sign bit
set, else a; for b < 0, 0 under 0xa and under 0xd ~a where b + a + 1 has its sign bit set, else
b [verified, pre-registered on constructed sign and overflow lanes].

The carry-in heads add a carry c, bit k of a 32-bit register for lane k: F0[f76_79] for S0 0x1e /
0x2a (sat_s(a + b + c)), 0x1f / 0x2b (wrapping) and 0x2c / 0x2d (sat_u), F1[f80_83] for S1 0x32 /
0x3d, 0x33 / 0x3e and 0x3f / 0x40 [verified, pre-registered; 0x1f and 0x33 with the register at four numbers, the
others at one]. An add's carry mask is its held result (below), so a **32-bit add per lane** takes three
words: the low halves' add (S0 0x20), then M one word later into F1[0] (f258 = 0), then the high
halves' carry-in head (S1 0x33 with f80_83 = 0) in the word after [verified, pre-registered]. The
stock programs run the same idiom within S0 (0x20, M into F0[0], 0x1f with f76_79 = 0) [verified
in the bytes and on the camera]; split across S0 and S1, each lane file's two ports serve one
product's two halves. S1's floor shifts of a : b add the same carry-in: ((a : b) >> n) + c, low
16 bits, with n = imm7 & 15 under 0x16..0x19 and n = F1[f84_87] & 15 under 0x1f..0x22 [verified,
pre-registered]; with c = 0 they are the plain floor shift, and what separates the four heads of
each group is open.

Lane masks (`fields.LANE_MASKS`): S0's per-lane condition is a 32-bit mask in a scalar
register of F0, bit k for lane k; S0's and S1's compare heads make one.

- **Select** [verified, pre-registered: 0x46 under two masks, the others under one]: with bit = bit k of F0[f72_75], S0 0x46
  gives bit ? b : a, 0x45 bit ? b : F0[f76_79], 0x44 bit ? b : imm16, and 0x4b the bit as 0 / 1;
  a lane port (or O under 4) takes them in the head's own word. 0x41 and 0x4d give b, 0x42
  imm16 and 0x43 F0[f76_79] in every lane [verified under one mask: no select seen]. None of
  these heads hangs the engine.
- **Compare** [verified, pre-registered]: S0 0x24 / 0x25 (a != b / a == b), 0x32 / 0x33
  (a < b / a >= b), 0x2e / 0x2f (b > / <= imm16), 0x30 / 0x31 (b > / <= F0[f72_75]), 0x12 /
  0x13 (b != / == imm16), 0x18 / 0x19 (b != / == F0[f72_75]), 0x20 (the carry of a + b), 0x1f
  and 0x2c (the carry of a + b + c) and 0x2a (the signed overflow of a + b + c) put a mask, bit
  k from lane k, on M's bus one word after the head under f274_275 = 0 (two words after S0
  0x20 no longer); S1 0x34 (the carry of a + b), 0x33 and 0x3f (the carry of a + b + c), 0x3d (its
  signed overflow), 0x41 / 0x42 (b > / <= imm7), 0x43 / 0x44 (b > / <= F1[f84_87]) and 0x45 /
  0x46 (a < b / a >= b) under f274_275 = 1 (`fields.S_RESULTS`). These order compares are
  unsigned; a signed one is the unsigned one on both operands XOR-ed with 0x8000 (S1 0x01
  against a broadcast 0x8000), which gave a < b and a >= b signed through S0 0x32 / 0x33 and S1
  0x45 / 0x46 [verified, pre-registered, four masks]. The signed max / min heads hand over a
  signed order compare, so one head and one M word make it [verified, pre-registered with ties]:
  S0 0x22 / S1 0x2e a < b, S0 0x23 / S1 0x2f a >= b; against a scalar or an immediate the max
  heads (S0 0x16, 0xe, S1 0x2c, 0x2a) b > x and the min heads (S0 0x17, 0xf, S1 0x2d, 0x2b)
  b <= x. The unsigned ones hand over none.
- **f274_275 names the unit** [verified, pre-registered]: with S0 0x52, S1 0x54, S2 0xe and S3
  0x24 in one word, M under 0..3 puts out S0's, S1's, S2's and S3's value; S2 holds no result.
  M takes a unit's result of the previous word while that unit has a new head in the M word
  [verified: S0 0x20 in consecutive words of a packed loop].
- **Into F0 and F1**: f255 = 0 takes M's bus into F0, f258 = 0 into F1 [verified], under
  f274_275 = 3 S3's held reduction too: S3 0x0's maximum into F0, then S0 0x19 against it gave
  the one-hot mask of the maximum's lane [verified, pre-registered]. In the stock
  programs every S0 0x13 is followed by a word with M under f274_275 = 0 and F0's port f182_185
  under f255 = 0 [verified in the bytes].

So a mask can be computed, kept, built in a scalar register and applied: S0 0x32, M with f255
= 0 into F0[k], then S0 0x46 with f72_75 = k gave max_u(a, b) as one program [verified,
pre-registered]. No select of S1's is known.
`semantics.lane_result` evaluates the select heads with the mask as the scalar "s".

The immediates by field (`fields.LANE_IMMEDIATES`, fitted, then pre-registered on fresh values,
S0's bit 5 set in a later cell: verified): S0
imm16 = f76_79 << 12 | f37_40 << 8 | f23_30; S1 imm7 = sext7(f80_83 bits 3..2 << 5 | f84_87 << 1
| f0); S2 imm7 = sext7(f53_56 << 3 | f88_91 >> 1); S3 imm14 = sext14(f96_99 << 10 | f65_68 << 6
| f31_34 bits 3..2 << 4 | f92_95). Register nibbles double as immediate bits; f80_83's bits 1..0
do not enter S1's.

A product's high half goes to S0's and S1's files through their first ports under 1
(`fields.S2_HIGH_HALVES`): of S2's seven multiplies 0x0 is the unsigned b x imm's 16 bits,
0x1 the signed b x imm, 0x2 the unsigned a x the signed b, 0x3 the unsigned b x t, 0x4 the
unsigned a x the unsigned b, 0x5 the signed b x t and 0x6 the signed a x the signed b
[verified; with t's bit 15 set 0x3 and 0x5 inferred: t was positive in every run]. With f155 / 1 and f159 / 1 in the word after S2 0x5,
S1's a : b is the 32-bit product and S1 0x29 (or 0x10) gives it scaled, rounded and saturated:
a fixed-point multiply by a scalar in three words [verified].

These pieces make layers that run exact from DRAM to DRAM as one job [verified, pre-registered,
every output byte]: a 3 x 3 convolution of a byte image with signed weights in F2 and 32-bit
accumulators per lane (S2 0x5's product halves into L0 and L1, the 32-bit add above, S1 0x28's
narrowing shift, S0 0x8's clamp, an X0 store), and a dense layer whose weights stay in the lane
memory, so that later jobs carry only their input. The convolution also runs as a loop of 27
words per 32-pixel output row, the multiplies one word apart, at about 5.4 ns per word [verified,
pre-registered, timed over several copies]. Layers chain inside one job: two convolutions and a
2 x 2 max pool (max_u of row pairs, then S3's even / odd lane splits 0xd / 0xc joined by max_u),
the intermediate rows staying in the lane memory between the layers [verified, pre-registered].

`semantics.lane_result(unit, head, a, b, scalars)` evaluates a row on 32 lanes, a None lane
where the row does not predict it, and `semantics.product_high(head, a, b, scalars)` the high
halves. `machine.Machine` runs them ("The datapath executor").

X0 and X1 are two copies of one ALU (`fields.X_FUNCTIONS`, verified by exact readout): X0's head
f302_307 on a = A[f100_104], b = A[f105_109], K = f105_109 << 6 | f2_6 << 1 | f1 (11 bits); X1's
head f308_312 on a = B[f110_114], b = B[f115_119], K = f115_119 << 8 | f9_14 << 2 | f7_8 (13
bits). The shared table is the op slot's compacted: compares against K and b, a +/- K, a +/- b,
-b, abs(b), b, K zero- or sign-extended, a | K << 16. X1 adds logic functions at 0x16..0x1b; its
heads 0x1c..0x1e are the register transfers, which put no result on the bus and change no
register. X0 gives no result at 0x16..0x19 and 0x22, 0 at 0x23..0x3e, and at 0x1a..0x21 the
immediate + 1 or b + 1 beside a lane memory load or store (below).

#### The lane memory and the store file

X0's heads 0x1a..0x21 load and store whole vectors of the lane memory (`fields.LANE_MEMORY`,
`fields.X0_LANE_MEMORY`): 0x400 vectors of 32 elements of 16 bits, addressed by vector number
modulo 0x400 [verified: the wrap pre-registered at 0x400 and through a second operand of 0x7fff;
the size inferred from five different contents below 0x400 and aliases above].

| heads | does | address |
|---|---|---|
| 0x1c, 0x1d | load one vector | A[f100_104] + sK, K X0's signed 11-bit immediate |
| 0x20, 0x21 | load one vector | A[f100_104] + A[f105_109] |
| 0x1a, 0x1b | store O[f69_71] | A[f100_104] + sK |
| 0x1e, 0x1f | store O[f69_71] | A[f100_104] + A[f105_109] |

A load's 32 lanes arrive exactly four words after the head, at any first lane port under enable
0 or at the store file's port; at lag 3 or 5, or with no head, the port writes nothing; one load
reaches all four first ports in one word [verified]. In the five stock programs every word with a load head has
such a port write four words later, and none lacks its head (379 words; verified in the bytes).
The store file O holds eight lane registers: the port f179_181 (the register) writes it under
f252_254 (the source: 0 a load, 1 S3's result, 2 S2's, 3 S1's, 4 S0's, 7 idle; 5 and 6 never
occur and leave O as it was [verified, pre-registered], `fields.STORE_SOURCES`), the moves tried in the head's own word and the
arithmetic results tried one word after it, and a store head writes O[f69_71] [verified]. A store head in
the word of O's port write stores the old content, one to three words later the new content; a
load one or two words after a store to the same vector reads it back, and a store up to three
words after a load of its vector does not change the load [verified]; four loads in consecutive
words land in four consecutive port words [inferred]. The lane memory keeps
its content across jobs within a boot [verified, pre-registered: vectors 0x40..0x7f read back after
later jobs]. Each head and its even / odd twin showed no
difference in address, lanes, element width or scalar result, and none writes A back
[inferred]; what the low head bit selects is open [it is no wait on the stream engine: a load
head beside a load in flight read the old content either way, measured].

The lane memory is the stock copy's buffer (`memcpy`): at placement 0 the copy's load puts
source byte i, zero-extended, in lane i % 32 of vector i / 32, and its store writes each
element's low 8 bits [verified]. A kernel with bulk input and output runs inside one stock copy:
the loaded vectors are there once the copy's word 74 has run (three vectors read, 8 lanes each:
word 74 is the load's 0x1d, which waits for it), own words between
74 and 75 compute on them, and 75..78 store them (`memcpy`; verified: a dense layer of 32
neurons x 64 inputs, DRAM bytes to DRAM bytes, exact). The store's element format, word 47's
value, is the number of elements per 16-byte line: 16 bytes, 12 a 10-bit pack, 10 a 12-bit
pack, 9 a 14-bit pack, 8 16-bit little-endian, 4 zero-extended to 32 bits [verified; the 10-bit
pack's bit order inferred]; 13 is no line count: the store writes zeros over the whole length
[verified, pre-registered]; 16-bit lanes also leave as bytes, interleaved by S3 0xa /
0xb with a shift by 8 [verified]. `semantics.LaneMemory` holds the content by
vector (`load`, `store`, `copy_in`, `copy_out`) and `semantics.lane_address(head, a, b, k)`
gives a head's vector; `machine.Machine` runs both.

What can be written today: an own loop of 16 passes, each incrementing an index into G and A,
loading two vectors into L2 through X0 0x1d, multiplying them with S2 0x2, putting the product's
halves into L1 through f155 / 1 and f159 / 1 and its low half into L3 through f175 / 1,
narrowing with S1 0x29 into O under 3 and storing it with X0 0x1b, and reducing the products in
L3 with S3 0x2, read through M into G and accumulated by op 0x35, gave the 32-bit sum of 512 products and every stored vector exactly [verified,
pre-registered].

T's bits stay in the positional fields, so every word has the same partition and the codec stays
exact; `TARGET` in the table composes T from `imm` and f128_132.

`Word.ctl(op, target, base)` assembles a control word: the op, f229 clear and, for ops 0..3, T
(over 13 bits raises). In a statement list, `Label("name")` marks the next word and
`Ctl(op, "name", base)` is a control op to it; `assemble(stmts, origin=N)` places the words at
word index N in two passes, so moving the origin moves every label target and nothing else. An
undefined or duplicate label raises `AssemblyError`; a word's own target stays absolute.
`labels(stmts, origin)` gives the label indices, and `source(program, name, labels, origin)` prints
the statements back (a `Ctl` where a target has a label). A call's return is implicit, i + 5;
`call_return_errors(program)` reports each op 3 whose return point lies past the program end.

### Data ops: the op slot

A data op holds its operands in five fields, the op slot: `op`, `imm`, the register `ra`
(f123_127), f128_132 and f120_122. `ra` changes only in a word that carries an op (a data op or a
control op 0..2; a call holds it), verified in the bytes across later firmware versions of the
same family; it is a base or a source, the result going through a PR port. The data ops fall into
families by the layout of the slot (`FAMILIES`; the formats are verified in the bytes, every
meaning is a hypothesis unless tagged):

| family | ops | operands | f120_122 |
|---|---|---|---|
| A | 0x0c..0x1a even | ra, imm13 signed | a store's datum register R[f120_122]; no effect on a load |
| A' | 0x0d..0x1b odd | ra, rb | as family A |
| B | 0x0a 0x0b 0x37 0x38 | ra, imm16 (imm15 in 0x37) | immediate bits 13..15 |
| C | 0x3e 0x3f 0x40 | ra, rb (no effect), imm5 | no effect |
| D | 0x1c..0x25 but 0x22, 0x36 | ra, imm16, sign-extended but zero-extended in 0x1e, 0x1f, 0x23 | immediate bits 13..15, 0 in 99 % of the words |
| E | 0x08 0x09 0x28..0x35 but 0x2d, 0x3a 0x3c 0x3d 0x41..0x43 0x47..0x49 | ra, rb | no effect but in 0x47, R[f120_122] |
| F | 0x44 0x46 | ra, imm13 | none in 0x44; R[f120_122] in 0x46 |

rb is f128_132; imm13 is `imm` | f128_132 << 8 and imm16 adds f120_122 << 13. The A ops are the
immediate forms of the A' ops at op + 1. In family C rb is encoded but had no effect on the
result (verified by exact readout).

The data ops (`OP_SEMANTICS`, each with its tier) compute on plain 32-bit values, with 32-bit
wrap; a compare gives 1 or 0. An earlier indirect readout, through the copy's load offset and
store count, showed immediates and results scaled by 16: that scale belongs to those two
roles, which use a register value in units of 16 bytes (`memcpy.READOUT`; inferred: r2 & 0x1080
through the count at len 0x1100 stored nothing, as a 16-byte unit predicts), not to the ops.
Verified by exact readout: 0x08 / 0x09 rb, 0x3c ra & rb, 0x3a ra | rb, 0x34 ra - rb, 0x35 ra +
rb, 0x33 -rb, 0x3d ~ra, 0x2f min signed, 0x2e max signed, 0x41 ra >> (rb & 31) arithmetic, 0x43
ra << (rb & 31), 0x28 / 0x29 ra <= rb, ra < rb unsigned, 0x2a / 0x2b signed, 0x30 / 0x31 !=,
==; against c, imm16 sign-extended: 0x1c !=, 0x1d ==, 0x20 <=, 0x21 < signed, 0x24 / 0x25 max /
min signed, 0x36 ra ^ c; 0x0b c itself, ra not used; 0x37 ra | imm15; 0x38 ra & c; the ring
pointer steps 0x44, 0x46 and 0x47 (216 points): s = zext(ra) + sext(R), R = 1 in 0x44 and
R[f120_122] in 0x46 / 0x47, L the low 8 bits of imm13 (0x44, 0x46) or rb (0x47), the result s +
L if s < 0, s - L if s >= L, else s. 0x49 and 0x48 multiply: (ra x rb) & 0xffffffff reaches
the ALU bus one word after the op, where a port of that word takes it, and the op's own word gets
nothing (`semantics.MULTIPLY_LAG`); what sets 0x48 apart is open, and stepping records the
product without delivering it. Inferred: 0x32 abs(rb), 0x2c max unsigned, 0x42 ra >> (rb & 31)
logical, 0x1e / 0x1f ra - imm16, ra + imm16 and 0x23 ra < imm16 unsigned with imm16
zero-extended, 0x0a ra | (imm16 << 16), 0x3e / 0x3f / 0x40 ra >> imm5 arithmetic, logical and
ra << imm5 (ra shifted, not rb): verified on the camera since (pre-registered), as
`OP_SEMANTICS` rates them.

Family A is local memory, 0x2000 bytes with a stack at its top and r0 the stack pointer, at the
byte address ra + w x imm13 modulo 0x2000 (`semantics.LOCAL_BYTES`), imm13 signed (verified by
exact readout: any ra reads at its low 13 bits, so no DRAM, register or lane state is reachable;
imm13's high bits, once read as a flag, are the offset's sign, which modulo 0x2000 gives the
same address as the unsigned reading); the register forms use ra + w x rb. The stores 0x18 / 0x12 /
0x0c write the low 32 / 16 / 8 bits of R[f120_122] (register forms 0x19 / 0x13 / 0x0d); the
loads are 0x1a 32-bit, 0x16 signed and 0x14 unsigned 16-bit, 0x10 unsigned and 0x0e signed 8-bit
(register forms 0x1b, 0x17, 0x15, 0x11, 0x0f). A load's datum goes to no register by itself: it
appears on the ALU result bus exactly three words after the load, where a word with
f272_273 = 1 or f270_271 = 1 takes it; the bus ORs it with a data op's result and with a
multiply's product arriving in that word, and a word with a control op, a store, a load or a
multiply takes it alone; consecutive loads deliver in consecutive words (verified by exact
readout). Stepping records a
load's read only and makes a store's bytes unknown, since R is not modelled; an access at an
address not aligned to its width has no semantics. The resampler's word that reads tap 1 is a
0x16 with imm 1; on the camera, with ra kept (one run each):

| op in its place | imm 0 | imm 1 | reading |
|---|---|---|---|
| 0x16 (stock) | c0 | c1 | halfword, imm in halfwords |
| 0x14 | c0 | c1 | halfword, imm in halfwords |
| 0x10 | c0's byte 0, zero-extended | byte 1, zero-extended | byte, imm in bytes |
| 0x0e | c0's byte 0, sign-extended | byte 1, sign-extended (two tap sets) | byte, imm in bytes |
| 0x1a | - | c2, the low 16 bits of the word | word, imm in words |

So the coefficient port keeps between 9 and 16 bits of what reaches it (a zero-extended 0xff
arrives as 255, a word as its low half).
`word.operands` decodes a data op's operands (`Operands`), and
`Word.data(op, Operands(...))` assembles one, rejecting operands outside the format; listings show
`op1a~ r5, #1` and `op3f~ r2, r2, #5`, and a slot field outside the format stays a field.

Every control op has four delay slots (`DELAY_SLOTS`, `Program.delay_slots()`): in the five stock
programs words i+1..i+4 of a control word at i are never a target or a control word (verified in
the bytes; across later firmware versions of the same family only short loop ends break it). The
reading, inferred strong, is that the slots always run and the transfer takes effect after them,
so a call returns to i+5. A program is "call 10, halt at 5": word 0 calls the job routine, whose
return comes back to word 5.

Listings start every non-idle word with its op name and confidence mark, follow a control op 0..3
with `-> T`, and mark delay slots with `|` after the index; `fenykl dsp fields` prints T's
definition, the op meanings and the delay-slot rule after the table.

### Timing and state across jobs

Not modelled by the emulator; measured with the job's completion time against loop pass counts.

- A word inside a loop costs about 5.3 ns, whatever it holds: idle words, scalar local loads, X1's
  divide, S3's reductions, X0 vector loads and taken branches over their slots all cost the same
  within 1.5 ns [verified, pre-registered]. A straight-line word costs about 62 ns the first time
  it runs; a word branched over costs nothing. A job adds about 100 us (the interrupt path), and
  single jobs jitter by up to 1.5 ms, so timings take the minimum over repeats.
- The stream engine moves about 2.2 KiB per us for byte loads and stores alike, with well under
  1 us per command besides the bytes; commands on different channels run one after the other
  [inferred from pass-count slopes]. Of 32-byte loads handed before the first 0x1d the first 19
  run and the rest wait until 0x1d's free room; none is lost, and each 0x1d counts one handed
  command, kind 2 included [verified, 32 commands and 32 waits] (`stream`).
- A job runs up to at least 7 ms: own loops of 0.4 to 7.0 ms all ended normally, at 5.21 ns per
  looped word plus about 105 us [verified, pre-registered]. The firmware gives this job type 8
  ticks of 1 ms before it aborts, so keep a job below about 7.5 ms.
- An own program's frame: program 16's words 0..9 (word 0 calls word 10, its delay slots set the
  stack and link registers, word 5 halts), the body from word 10, then a halt and four idles; of
  those four only three run (a halt's tail). The smallest such image is 15 words with no body
  [verified, pre-registered]. Program 16's words 10..12 are not needed: the body may start at 10
  or replace them, and sets its own registers [verified, pre-registered]. Images that change
  words 0..9 were not run on the camera; idling word 0 of the stock copy ends the job at word 5
  without running the routine [verified].
- Nothing clears the DSP's state between jobs: every register file (G, R, A, B, F0..F3, the lane
  files, U, O), the lane memory and local memory keep their content from one job to the next,
  except what the next job's own words write; a stock copy of n bytes overwrites lane-memory
  vectors 0..ceil(n / 32) - 1 and its own registers [verified, pre-registered]. After power-up
  they hold SRAM content, stable within a boot and a few bits apart between boots [verified].
  The stream engine's channel registers are the exception: they do not survive a job (`stream`).

## Confidence

`verified` (read on the camera or in the bytes), `inferred` (one step beyond), `hypothesis`. A
field's confidence qualifies the field itself, its bits and kind; facts about single values or
words carry their own wording in `fact`. The rules:

- `assemble()` rejects words whose constant fields are off, and emits one
  `HypothesisFieldWarning` that lists every hypothesis field the program moves off idle;
- listings mark verified values `!` and inferred ones `~`; unmarked values are hypotheses;
- the emulator gives every output byte the weakest confidence of the semantics that produced or
  moved it (the field's own confidence does not enter); see the rule below.

### The emulator's confidence rule

Every semantic carries a confidence: each word class (`WordClass.confidence`), each part's
outcomes (`Part.seen`), each role's table and use (`Role.confidence`, `seen`, `beyond`), each
register fact. What was seen on the camera exactly is verified; what the model extends from it
is weaker:

- **Positions.** A semantic at its stock position is at most inferred, at any other position a
  hypothesis: an undecoded stock word is a row at its stock index only (inferred), and at
  another index it is no row (strict mode raises, lenient mode runs the stock word there); a
  transfer whose role the sequencer assigns away from the role's stock word is a hypothesis;
  the idle word is verified anywhere. Stock words that inserted words move keep their rows at
  hypothesis; an inserted data op has its op semantics' confidence ("Inserted words").
- **Outcome tables.** What a role's table (or a part's swept register, or an idled part) gives
  is verified at the job lengths it ran at on the camera (`seen`, 1 KiB unless stated) and
  inferred at others.
- **Uses.** A role's use of its register's value (address, offset, count, format) is inferred,
  at its stock register too, and never stronger than the register's fact; word 76's use at
  another register is a hypothesis beyond the lengths it ran at.
- **Combinations.** A job that differs from the stock program in two or more words is at most
  inferred, but for the exact field values of program 13's loop-head window that ran together
  on the camera ("The loop-head window").
- **Lenient mode.** An ignored field, or a route nibble off the stock transfers' values, makes
  the job a hypothesis: its loaded, stored and zero-filled bytes alike.

Per output byte the emulator takes the minimum: the loaded bytes get the weakest of the semantics
that ran (rows and transfers), the parts' outcomes, the caps above and the load's roles (66, 67, 68); the stored bytes
the weakest of the buffer bytes they come from and the same with the store's roles. Since no word
of program 16 is decoded, every byte it copies is at most inferred, the stock copy's too.
`JobResult.taint` qualifies what this job did: the weakest of the outcomes that decided its
status and whether it stored (an idled word, a role's register) under the caps, and of the store
when it stored; with no deciding outcome, the load's and the store's. Stored bytes can be weaker
still through the buffer they come from (a held buffer of an older job).

## The emulator

```python
from fenykl.dsp import commands
from fenykl.dsp.emu import Boot, Executor
from fenykl.dsp.memcpy import Copy16Model

model = Copy16Model.from_image(image)  # the stock E-M10 II .BIN
boot = Boot()  # BootParams: r14, r29, r21's format; None = unknown
boot.put(src, data, "src")  # known DRAM bytes, with a source name
result = Executor(boot, model).run(commands.copy(model.program, src, dst, len(data)))
out = boot.read(dst, len(data))  # Bytes: data, known, taint, src (provenance), off
```

A `Job` is a program plus the bytes its command's CPU setup writes to local memory
(`commands.copy` for cmd 0xF: src, dst and len at 0x9D000000..08). State persists across the jobs
of a `Boot`: local memory, the buffer between load and store (position-addressed, reaching below
index 0), the resampler's held line pairs, the register file and the DRAM view. `back_firmware(boot, image)` maps the main code
of a stock `.BIN` for loads from firmware text. A byte is known or UNKNOWN, and its source says
where it came from or why it is unknown; the emulator never guesses a byte.

The model covers each word of a job at its index (`model.cover`): the idle word; X1's register
transfer (head 0x1c or 0x1d) at any index (matched by its signature, every field idle but the
register groups, the route nibbles and the head); the row of the undecoded stock word at its stock index
(`OpaqueClass`, which leaves the measured operand fields free); and the stock halt at another
index, which acts where stepping reaches it (below) and is a hypothesis elsewhere. A stock word
at another index than its own is no row, unless inserted words moved it there ("Inserted
words"). The model derives the rows, the transfers' register contexts and their reference
values from the stock program it is built from, so no stock word is part of the code. At the
end the sequencer gives the transfers their roles (the stock word 66 loads, 67 addresses, 68
offsets, 73 counts, 76 offsets the store, 47 formats it, ...) in the
order of the transfers (`Sequencer`, inferred: the roles followed counted transfers moved by one
index on the camera, and a counted transfer at word 78, the word before the return, stalls the
job) and the job resolves into a status, one load and one store. The rows are outcomes of whole
jobs measured with every other word stock, so they are resolved by index, not along the control
flow: an idled control word (0, 5, 45, 79) has its row's outcome, and the words of the loop
50..57, which no stock job runs, keep their rows too.
An op 7 off the stock halt ends the job where stepping reaches it (`Copy16Model._halt`):
before the stream words 58..79 with status 0 and dst not written; after the stream started it
raises `UnknownSemantics`. Some timeouts come after the store (`Outcome.STORE_TIMEOUT`: idled
5, 28 or 79, 45's target moved to the loop's top 50, bit 6 of 65's immediate cleared): dst is
written, then the job never completes. The guard 45 skips the loop 50..57 in every tested job,
so the loop's words are known only with the loop skipped: an edit of 28 or 45 together with one
of 50..57 has the measured outcome of `memcpy._LOOP_ENTRIES` (45 and 53 idled: dst not written,
status 0) and no semantics otherwise.

Strict mode (the default) raises `UnknownSemantics` for a word nothing covers, naming the fields
that differ from what covers the stock word at its index, and for a role at a register without
registered semantics, and for a transfer whose route nibbles differ from the stock transfers'
values. `Executor(boot, model, strict=False)` runs the stock word at such a word's index,
ignores every field that differs from it and taints every byte the job loads or stores
"hypothesis"; stock transfers run so yield to the transfers of their channel that the job
carries, where they would overfill it (a transfer moved onto another word's index): as many as
overfill it, the first ones in program order, so that a transfer moved before them leaves
the later ones in their stock roles.

### Inserted words

A job longer than the stock program that is the stock program with words inserted (and some
edited in place) is covered by content (`memcpy.align`, `Copy16Model.cover_job`): its words
are matched to the stock words with the control targets left out, an insert into a run of equal
words counting as placed after the run. Each stock word is covered at its stock index, its
target moved back through the match, so a target that follows its word (labels, or every
target at or above the insert plus one) gives the stock word, and one left at its old index is
an edit of the word, which has no row. The job resolves in the stock index space: the
results name the stock words by their stock indices, the inserts by their job indices (a note
says where they are). An insert counts as an edited word for the combinations rule, and stock
words off their stock indices make the job a hypothesis.

An inserted word is covered by its content: the idle word, or a data op whose fields off idle
are all in the op slot and the PR ports, at its op semantics' confidence (`OP_SEMANTICS`, or
the executor's `ops`), when the semantics covers its operands. A load is recorded only (where
its datum goes is open; with a PR port enabled it is UNKNOWN). A result goes through the word's
PR ports (`semantics.destinations`), and only right after stock word 64, the site where ops were
measured (`memcpy.ENGINE_AFTER`): there the operands r2 = (len + 0x1f) / 16 and r7 near 0x4000
are known (inferred; r7 at len 0x2000 and 0x4000 only), a port-272 result feeds later
inserts' operands three words on, and a port-267 result, times 16 (`memcpy.READOUT`), is what
words 68 and 73 read at that register four or more words on; other transfers reading it raise. A load offset or count that the
model cannot place from such a value (unknown, negative, two placements, beyond the buffer, a
count off 16-byte steps or above len) raises. Every other insert is UNKNOWN, naming the op or
the fields: an op without semantics, operands outside them (an unaligned local access), a
result with no
decoded port or off the site, a local memory write, a register transfer (its slot is not
modelled). These raise in both modes: an inserted control
or halt word, an insert in a control word's four delay slots (it pushes a stock word out of
them), a target on an inserted word.

### The resampler

```python
from fenykl.dsp.resample import Resample13Model
from fenykl.dsp.taps import Rounding, Taps

model = Resample13Model.from_image(image)
taps = Taps.of([(-8, 264, 0, 0)] * 16)  # phase p = taps 0..3, s16, 256 = 1.0
taps = Taps.from_floats(
    [(-0.03, 1.03, 0, 0)] * 16, Rounding.HALF_EVEN
)  # each tap rounded
boot.put(src, planes, "src")  # three planes of 64 x H bytes at stride 64 (format 0x306)
boot.put(table, entries, "table")  # H x {s32 a, u32 b}: source line and step in 16.16
job = model.job(taps, src, dst, table, w, h)  # the stock words, these tap words
result = Executor(boot, model).run(job)
```

`predict(model, taps, image, [(a, b), ...], w)` runs that job on a fresh boot and returns a
`Prediction`: `dst` (the footprint as `Bytes`), `confidence()` per byte (None if unknown) and
`unknown()`, a mask per reason (the source's name: `resampler bottom`, `resampler right`,
`not written`, ...; the source's fact says why).

`commands.resample` writes what cmd 0xC's setup writes: the program's constant block (the 32 tap
words the setup copies from DRAM 0xA324BC00, the item's constants) to local words 0x00..0x1F and
the request to 0x742..0x747; the table stays in DRAM. Own taps are a program with other constants
(`model.job` builds it from the stock words). Word 2p of the tap words is phase p's tap 0 | tap 1
<< 16, word 2p + 1 tap 2 | tap 3 << 16. `Taps.from_floats` rounds each tap on its own and does not
renormalise a phase to 256. No word of program 13 is decoded: a job's word is covered at its index by
the stock word, a measured edit of it or the window's coverage rule, and the law (`resample.py`'s docstring) applies to the stock program
as it is. Every byte the law computes is at most inferred, a law measured from outside;
hypotheses: u0 held into [-16, 16) (b 2), a phase between the 16, at b != 1 a tap outside
-637..2048 and in an edited program a tap outside -1840..2048 (the taps that ran there; with the
stock program at b = 1 the whole s16 range ran) on the plane-0 bytes it reaches, the held lines after a job that is not an H 16 identity.
A partial column block is unknown unless the job is an H 16 identity with a unit phase-0 kernel.
Bytes the law does not give (the bottom edge but for the window's last line on x-varying
sources, the right edge, a fractional a with b != 1, W < 64 with
b != 1, an odd H above 1) are UNKNOWN with a source that says why. The curated camera vectors are in
`tests/data/dsp/resample/` (jobs with their taps, table and source image, an edit as {word, kind};
dst segments).

#### Edits

`resample_edits.FACTS` holds the edits measured on the camera: a stock word with some bits set to
the idle word's (`Idle`: the grid and C2's free bits, the grid, C2's free bits, the op bits, one
bit) or one field set to a value (`FieldValue("f211_215", 4)`: a register renamed;
`FieldValue("f57_60", 15)` at 439), and per job class (`Probe`: a flat source or not, the
step b, W; H 16) what each plane does: the stock law (`Stock`), unknown (`Changed`), or a law
(`Operands`: plane 0 with each tap multiplying another tap's line, nothing or a bounded value;
`Phase`: a chroma plane sampled at another horizontal phase; `Substitute`: plane 0 with a tap's
product replaced by the boot's tap-0 substitute, below). These rows hold what the loop-head
window (below) does not run; a field edit the window runs has no row.
`FieldValues` sets several fields of one word at once (`set_fields(op=0x10, imm=0)`). The model
covers an edited word at its index by its value: a measured edit there, or the window.

Words 439..442 hold one S2 operand set per tap, 0..3. f88_91 selects the tap's coefficient
with one map in all four words (3, 2, 4, 5: c0, c1, c2, c3; each word's stock value is its own
tap's; pre-registered and verified at each word). At 439, f57_60 selects tap 0's source line
(5, stock, its own; 4 tap 1's), and f88_91's 0 and 1 select none of the four: post hoc tap 0's
coefficient reads as p and 16 - p (p the phase in 16ths, the chroma planes' weights; inferred,
not modelled), so their plane 0 is unknown (rows). f57_60 = 5 at 441 and 442 reads plane 1's
line a + 1 (verified), at 440 too (post hoc, inferred). Idling
439's grid A (or the S2 triple) replaces tap 0's product by a per-lane value added without the
multiply, the **tap-0 substitute** (`Boot.substitute`). Idling 440's or 441's grid A drops tap
1's or tap 2's product; it ran only on boots where the substitute was 0, so the model gives
the drop there and unknown bytes elsewhere (`Substitute(tap)`). Idling 442's grid A changes plane
0 along x, and its plane 0 is unknown. Its value at boot varies per boot, so it is unknown on a fresh
boot and every such prediction there is unknown. A grid B idle of word 433 on a flat source at
W 64 sets it from its boot value to 0 (`SUBSTITUTE_MOVER`, inferred: three boots), where the idle
gives the drop of tap 0's product; the edits seen to keep it (`SUBSTITUTE_KEEPERS`) and stock jobs
keep it. Any other edited job makes it unknown, as does `Boot.forget_held` after a job that
raised, and from there the grid B idle does not bring it back to a known value: grid edits that
contain it left a non-zero moved value in place. `model.job(..., edits=[(439, GRID_C2)])` and `predict(..., edits=...)` apply them;
`Resample13Model(program, facts)` takes another outcome table.

Single C2 fields have rows too, plane-level outcomes. A port enable at its idle value 3
changes one plane (verified): at 439 f244_245 gives the bytes of idling its port, at 440 and 448
two enables give that plane of their word's whole idle (post hoc the port's register keeps its
older content, inferred). Idling f248_249 is the whole idle of the store word 533, and of 529
and 558 but for single lanes (inferred). S0's f41_44 and f76_79 at 446 and 448 change plane 0
only: f41_44 feeds a saturation select, f76_79 = v sets bytes to 255 by a per-lane mask that v
selects (post hoc laws, inferred; not modelled). Where an edit exposes state that no lab job
writes and that differs between boots (439's S2 head idle or 8, 557's head or its port 175
idle), that plane is unknown (`PER_BOOT`), never a value. Rows that ran in a copy of the program
placed elsewhere count as in place: five field edits gave the same bytes there.

- one edited word that is a measured row, on a class it ran on: its planes; `JobResult.edits`
  names it, verified (the bytes stay at most inferred, through the law);
- else a job whose every changed field the window covers, in any number of words: the window's
  bytes; `JobResult.edits` has one entry per changed field at its scoped tier, under the
  composition cap (below);
- anything else on a class or with words the rows and the window do not cover together (a row
  word with another edited word among them): dst unknown ("resampler edit") in strict mode, the
  stock law as a hypothesis in lenient mode;
- a word that is no row and whose changed fields the window does not cover: `UnknownSemantics`
  (strict), the stock law as a hypothesis (lenient).

An edited job forgets the held lines, and its partial column block is unknown.

#### The loop-head window

`Resample13Model` takes its b = 1 bytes from the field-level executor (`exec.run_window`) over
words 430..447 (`resample_window`): the stock program's term lists for a stock job, the edited
window's for a job it covers; a row's own law (`Operands`, `Phase`, `Substitute`) runs on the
stock kernel. Each term reads its source line in any
plane. The window runs once per job on symbolic values: the tap loads put `TapBytes` of the line's phase into the
load latch (3 words on, hypothesis), f198_201 writes the latch into a coefficient register
(the scalar file F2 that f261_262 = 2 writes from the ALU bus, inferred; that C is F2 a hypothesis),
the operand write ports put each word's anchored source line (`Row`) into a register, and each
S2 head-5 word adds C[f88_91] x R[f57_60] to its plane's accumulator (the window model's
reading, hypothesis: on the camera with own programs head 5 is the signed b x t, its low half
the unit's lane result and its high half taken by S1's file's first port under 1, the pair
narrowed by S1's double-width shift; `fields.S2_HIGH_HALVES`). Every changed field must
be a semantic or a don't-care (a held field under an idle owner), else the executor gives no
terms (`Uncovered`). A stock value at its stock index has the law's tier on every job class;
an edited value its measured cell's tier, the mechanism's off its cells, and no semantic off
the job classes it ran on. The window's classes (`resample_window.CLASSES`) are flat sources and
x-varying ones at b = 1, W 64: on x-varying sources the cells that ran there are verified (88 =
1 and 5 at 439, 4 at 440, 3 at 441 and 442; 57 = 4 at 439, 5 at 440..442: every byte of the
three planes as the per-pixel law gives it, each column reading its own source column), and
every other cell, don't-care and mechanism holds at most inferred; b != 1 stays off the
classes. A source is x-varying there as the cells ran on it: in each plane, the 64 columns over
the lines the table reaches are pairwise distinct (`resample._x_varying`); on a source neither
flat nor x-varying the cells are at most inferred. Unless the job edits one semantic at a measured cell, an edited
register value that reaches a word reading it without a semantic (`exec.readers`; words shown
inert at b = 1 aside) makes the window unknown.

Composition (`resample_window.composition_cap`): five compositions ran and gave the composed
bytes [verified] (`COMPOSED`): f57_60 with f88_91 in one tap word, 57 = 4 with 88 = 5 at 439
and 57 = 5 with 88 = 5 at 441 (the tap takes the selected coefficient times the selected
operand), and f88_91 at two words, 88 = 2 at 439 with 88 = 3 at 440, 88 = 5 at 441 with 88 = 4
at 442 and 88 = 3 at 440 with 88 = 2 at 444 (each acts in its accumulation as it does alone).
Jobs with exactly those (word, field, value) sets keep their fields' tiers on flat sources; any
other job that edits two or more semantics, and those on x-varying sources, is at most
inferred.

On x-varying sources at H 16 the window's jobs read plane 0's lines 16 and 17 on plane 0's
output line 15 (a = 15) both as plane 1's line 0 (`resample.LAST_H`) [verified]; elsewhere (other
output lines and planes, other sources) a tap on a line past H - 1 stays unknown (`resampler
bottom`).

### Stepping

```python
from fenykl.dsp.emu import Boot, Executor, start_copy
from fenykl.dsp.memcpy import copy_branches

job = commands.copy(model.program, src, dst, length)
# start_copy: the copy's register facts in the transfer registers, after the job's setup
flow = Executor(Boot(), branching=copy_branches(length), strict=False).step(
    job, start_copy
)
print(flow.runs())  # 0, |1..4, 10..27, |28..31, 32..45, |46..49, 58..79, |80..83, 5
```

`Executor.step` follows the program counter from word 0: a call at i goes to T and returns to i + 5, a
return goes back there, brf, br and a loop end go to T when taken, op 7 halts at any depth; the four delay
slots of every control op run before its transfer (`DELAY_SLOTS`). Each executed word is a
`Step` (index, op, operands, delay slot, decision or effect, confidence); `Flow` holds them in
order, with how the run stopped: halt, an undecided branch, or the step limit.

- **Branches** (`semantics.Branching`): no condition of ops 0..2 is known, so the default
  `Given()` decides none. `Given({index: taken or [per visit]})` takes the caller's decisions;
  `memcpy.copy_branches(len)` gives the copy's two forward branches as the camera runs show them
  (27 not taken, 45 taken, also at odd src, dst and len). `RaTest` decides brf, br and the loop
  end on ra == 0 or ra != 0, in the op slot's registers or the transfer registers, the loop end
  testing ra as it is or after decrementing it; its brf and br defaults are the measured rules,
  while the measured loop end (ra > 0, ra - 1 written back through a port) is not expressed. On the copy's transfer
  registers it decides nothing: word 14 names 27's r7 through a port whose destination is open.
- **Data ops** (`semantics.OP_SEMANTICS`, each with its confidence): the family A loads 0x1a,
  0x16, 0x14, 0x10 and 0x0e record their read (the datum's delivery three words on is not
  modelled), the stores 0x18, 0x12 and 0x0c make their bytes unknown (R is not modelled), the computing ops send their result through the word's PR
  ports (`semantics.RESULT_PORTS`: 267 / 1 to the transfer registers four words on, 272 / 1 to
  the operands three words on, unknown until then; exact readout shows the next word reading a
  272 / 1 write, which these latencies do not model yet), and the idle op does nothing in the
  slot. This is the stepper's own model; with `machine.Machine` as its datapath the loads
  deliver, the stores write, and results reach their ports at the measured lags (`machine.py`). A
  result with an open destination (no port, or another enable value) raises in strict mode and
  makes the open ports' registers unknown in lenient mode. Every other op has no semantics:
  strict mode raises `UnknownSemantics`, lenient mode records it in its step and makes its ra
  and its ports' registers unknown. An undecided branch raises in
  strict mode and stops the run in lenient mode. Every taken control op jumps after its own
  four following words, also a control op in a taken control op's delay slots and one whose
  target is one of its own slots [verified for a taken br in a br's first slot and a loop end
  into its third slot; any other such step is inferred at most]. On the camera
  op 3 is a jump and op 4 jumps to the word G[f128_132] names [verified]: with a `datapath` the
  stepper takes op 4's target from that register (undecided while it is unknown) and keeps no
  stack.
- **Local op 3** (`Program.local_op3()`), plain stepping only: a forward op 3 after word 0 with
  no return between it and T, a jump on the camera. The plain stepper, which holds no link
  register, infers calls and returns with a stack of its own: such an op 3 runs as a call by
  default, or as a jump with no link with `Executor(..., local_op3_jump=True)`. A return that pops a local op 3's link, or
  finds no link, raises `LinkError` (an `UnknownSemantics`): no return of its own lies before
  that T, and stepped as calls such a link re-ran a loop's tail.
- **Registers**: the operands (`Boot.slot`, the file G) and the transfer registers
  (`Boot.regs`, the file B); R and A are not modelled.

Stepping models neither the register transfers nor the datapath, so it says which words run in
which order, not what they move. `fenykl dsp flow [--len N] [--branch I=0|1]` prints the copy's
flow.

### The datapath executor

```python
from fenykl.dsp.machine import Machine

boot = Boot()
m = Machine(boot)  # or Machine(boot, carry): what an earlier job of the boot left
Executor(boot, datapath=m, limit=5_000_000).step(commands.copy(program, src, dst, n))
boot.local.read(
    LOCAL_BASE + 0x40, 4
)  # what the program stored, known or not, with its tier
```

`Machine` runs every unit of a word (`Executor.step` keeps the control flow and asks the machine,
a `Branching`, to decide ops 0..2 on G[ra]). One clock rule: every unit reads the state from
before the word and every port write lands at its end; a unit that reads a register a port
writes in its own word gets the old value (verified for every register file). Buses carry a
value in the word it arrives in and OR what arrives together: the op result in its word, a
product one word later, a local load's datum three words later, a lane load four words after its X0 head, a lane
head's result `LANE_LAG` words after the head for that word only (a head without a measured lag
at both 0 and 1, as a hypothesis), S0's and S1's masks on M one word after the head, S3's
reductions in the third word only. A port with nothing arriving writes nothing. Values are
`Sv` (32 bits) and `Vec` (32 lanes of 16 bits), None where not known, each with the weakest tier
of what made it. Strict: an op without semantics, a transfer (X1 0x1c, 0x1d) without a
stream, an unaligned local access, a store at an unknown address, two
ports on one register and M combinations not measured raise `UnknownSemantics`; a head without a
rule, or U's port beside a head whose U content is open, makes what it writes unknown; U's port
with no S3 head writes nothing [verified]. Across
the jobs of a boot the lane memory and every register file are carried (`Machine.between_jobs`;
verified on the camera for each file); a program that runs between two jobs, and what of the
lane memory survives it, are the caller's part. Operand use is declared per rule (`LaneRule.reads`,
`XRule.reads`, `ScalarValue.reads`). M under a unit after a head whose result there was not
read is unknown (`fields.S_OWN_OPEN`, `S_NEXT_NONE`, `S_NEXT_VALUES`), but for an S3 extract
after an extract, which reads its own lane (measured); "nothing on M" behind a head
without a result there is verified from two words after a head on under every unit, also after
a reduction (but its first word) with other heads between, and, with one unit's head in the
word, in the head's own word and one word after under every unit, but under S0..S2 in an S3
head's own word; inferred there, beside two units' heads in one word and with no head before.
An S3 extract ORs with a reduction in the reduction's first word and comes alone later
[verified]. Behind a halt the next three words fetched run and the job ends: a taken jump due
among them puts its target's words there, one due after them is not taken (`TAIL_SHAPES`:
verified for op 3 in slot 1 and op 4 in slots 2 and 3, inferred for op 3's fitted slots 2..4
and op 4's slot 4, whose writes are capped at inferred); a halt first and an op 3 or op 4
second among them are not taken [verified, with no jump pending], a halt third ends the job
[inferred]; other control words there and other pending jumps raise.

`stream.py` gives X1's transfers to the stream engine (`Engine`, a fresh one per job: its
channel registers do not survive a job; a job with transfers and no stream raises). The engine
sees only the values X1 0x1c hands [verified]: a key 0x21 | channel << 6 | register << 11 and one
value, a block key 0x20 | channel << 6 and eight values (registers 0..7), or a command channel
<< 6 | kind (0 load, 6 store, 1 nothing; that a 0x1d counts kind 1 is inferred, other kinds
raise) and (from, to). Per channel r0 names the memory (1 the
lane memory, 2 the local memory), r2 its elements per unit and r3 the units per row, r5 the
elements per 16-byte DRAM line (the element width 128 / r5 bits), r6 the lines per row, r7 the
rows (verified up to 4 rows on a load, 2 on a store), r1 and r4 the row pitches in bytes on the
memory and the DRAM side; DRAM addresses are floored to 16 bytes, lane offsets to their vector
[verified]. A command starts when handed and a 0x1d waits for the oldest outstanding one
[verified]; a lone 0x1d in a job that handed no command returns [inferred]; what is outstanding
completes at the job's end (`finish`, called by `Machine.between_jobs`) [verified for a store,
inferred for a load]. A load's destination is unknown until then and its DRAM source is read
when handed; a store takes its source when handed [inferred where the source is rewritten
before the store completes]; lane reads beside an outstanding command are a hypothesis; a
command with a register not handed moves nothing [inferred]. What was not measured takes the
engine off script (`Engine.off`: the lane memory unknown, later destinations unknown); a
destination that cannot be bounded, and any value after the grammar is lost, raise. X1 0x1e is
no transfer: its signed quotient a / b is on X1's result bus four words after the head
[verified for a >= 0, b > 0 and one divide at a time; inferred with another in flight].

### Adding a semantic

- **A register value**: add or refine a `RegisterFact` in `state.py` (an exact value or an
  interval, from the job's request or the boot parameters, with its confidence). A role that
  reads the value then works at that register without further change.
- **A role's outcome at a register** (seen on the camera): add the register to the role's
  `table` in `memcpy._CHANNELS` (and the job lengths it ran at to `seen`), or to
  `memcpy._FORMATS` for word 47.
- **A new use of the register value**: a new `semantics.Use`, handled in `Copy16Model._role`.
- **What a stock word does without it**: its `Part` in `memcpy._PARTS` (`token`, `missing`
  outcome and the fact); a swept field goes in `operand`, by value, with its key in
  `operand_field` (xreg by default; word 60's f76_79 copies at 0 only); single edits of other
  fields go in `flips`, by the bits flipped against the stock value (45's target, 65's
  immediate), so that no stock value is part of the code. An edit of two fields of one word has
  no semantics.
- **A field fact of program 13's loop-head window**: a `FieldSemantic` in
  `resample_window.SEMANTICS` (the fields read as one value, the context, the tier off its
  cells), its measured cells in `_cells` with their tier, and a composition's (word, field,
  value) set in `COMPOSED` only once it ran; a fact no semantic gives (an idle, a value with
  no mechanism) stays a row in `resample_edits.FACTS`.
- **A new field semantic of program 16**: a `WordClass` subclass with `match` (field values
  that identify it), `operands` (fields it reads or ignores) and `act`, which records into the
  job's `Trace`; have the model's `cover` run it. Every field outside `match` and `operands` must be idle.

Each semantic carries its confidence, and a test vector should come with it: the curated camera
vectors live in `tests/data/dsp/` (jobs, expected status and dst with UNKNOWN regions).
