"""The shapes `machine.Machine` runs at verified because they ran on the camera: the timing and bus
rules of the datapath beyond the encoding (`fields`). A shape outside them runs at inferred or
lower."""

LOAD_CHAIN_HEAD = 0x1D
"""The X0 lane load head of the measured load chains (`LOAD_CHAINS`)."""
LOAD_CHAINS = ((1,) * 15, (2,) * 31, (3,), (4,), (1, 3, 1, 3, 1))
"""X0 `LOAD_CHAIN_HEAD` loads in flight together (each issued at most `fields.LANE_LOAD_LAG` words
after the last) that land as each would alone, as the words between their heads, from the chain's
first load on: each vector at its first lane port `fields.LANE_LOAD_LAG` words after its head
(verified on the camera, pre-registered): sixteen in consecutive words (into L3 and L2, and a
matrix product's rows); up to 32 two words apart (one vector per two words, beside and between
lane stores); two three or four words apart (a head in the word the first load lands); pairs in
consecutive words three words apart, six loads (a two-row convolution). A chain that begins as one
of these counts (a load's tier is fixed as its head runs). Other loads in flight together (longer
or mixed chains, the other load heads) were not measured: inferred."""
DIVIDE_CHAINS = ((1, 1, 1), (2,), (3,), (4,))
"""X1 divides in flight together (each issued at most `fields.DIVIDE_LAG` words after the last)
whose quotients land as each would alone, as the words between their heads, from the chain's
first divide on (verified on the camera, pre-registered): four in consecutive words, and a second
one to four words after the first. A chain that begins as one of these counts; other divides in
flight together were not measured: inferred."""
FIRST_PORT_WINS = (("s0", 0x41), ("s3", 0x19))
"""Both ports of a lane file naming one register in one word: the first port's value (measured on
the camera in L0, L1, L2 and L3, the first port taking S0 0x41's result, the second S3 0x19's,
both in the head's own word, each head alone in its unit). Other sources and heads, and either
port taking nothing, inferred."""
