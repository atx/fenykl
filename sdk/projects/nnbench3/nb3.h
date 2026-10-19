/*
 * nnbench3: trained ternary layers as generated code split into I-cache-sized blocks of output
 * channels (fam_tsplit.c) against pair_tab_asm. nb_gen.c / nb_gen.S / nb_gen.h are written by
 * ../nnbench/gen_kernels.py from a fenykl-nn handoff directory.
 */
#ifndef NB3_H
#define NB3_H

#include "../nnbench/nnbench.h"

#define NB3_LAYERS 10u	/* model layers L0..L9 */

/* A trained layer: weights [o][i][ky][kx], bias with the rounding term, shift. */
struct nb3_layer {
	const int8_t *w;
	const int32_t *b;
	const uint8_t *s;
};
/* Indexed by the layer number, the last case parameter (the plan's cases come from nb_gen.h). */
extern const struct nb3_layer nb3_layers[NB3_LAYERS];

/* One variant of a layer: nblk consecutive nb_gens entries, each writing all output pixels of its
   block of output channels, 4 per call (gen_kernels.py body_tern). */
struct nb3_split {
	uint32_t p[NB_NP];
	const char *var;	/* the tsplit variant */
	const struct nb_gen *blk;
	uint32_t nblk;
};
extern const struct nb3_split nb3_splits[];
extern const unsigned int nb3_nsplit;

#endif
