---
name: Syndrome Extraction (Constant-Time Byte Position Finding)
source: optimized-routines string/aarch64/memchr.S, strchr.S
layers: [microarchitecture]
platforms: [arm]
keywords: [syndrome, byte position, NEON, rbit, clz, mask, memchr, strchr, find byte]
---

## Problem

After a NEON comparison (`cmeq`) identifies which bytes in a 16-byte vector match a target, you need to find the position of the FIRST matching byte. The naive approach extracts each lane individually (`vgetq_lane_u8`) and checks in a scalar loop -- 16 extractions + 16 compares + up to 16 branches.

ARM's optimized-routines uses a "syndrome" technique: apply a magic constant as a position mask, reduce with pairwise add, move to a scalar register, then `rbit` + `clz` to find the first set bit in constant time. This converts a variable-length search into a fixed 6-instruction sequence regardless of where the match is.

## Detection

- NEON comparison followed by lane-by-lane extraction to find which byte matched
- `memchr`, `strchr`, `strrchr` implementations with post-comparison scalar loops
- Any pattern: "I know one of these 16 bytes matched, but which one?"
- Code using `vmaxvq_u8` to detect if any match exists, followed by a loop to find where

## Transformation

### Core syndrome technique from ARM's memchr.S

The key insight: use a carefully chosen constant mask where each byte encodes its position. After AND-ing with the comparison result and reducing to a 64-bit scalar, the position of the first set bit directly encodes the byte index.

```
// Step 1: Compare -- which bytes match?
cmeq  v0.16b, v0.16b, v1.16b     // v0[i] = 0xFF if match, 0x00 otherwise

// Step 2: Apply position mask
// The mask 0x40100401_40100401_40100401_40100401 encodes 2 bits per byte
// position, arranged so that after pairwise reduction, bit positions
// map to byte indices
and   v0.16b, v0.16b, vmask.16b  // mask off position-encoding bits

// Step 3: Pairwise add to compress 16 bytes -> 8 bytes -> 64 bits
addp  v0.16b, v0.16b, v0.16b     // 16 bytes -> 8 bytes (in low 64 bits)

// Step 4: Move to scalar register
fmov  x0, d0                     // 64-bit syndrome value

// Step 5: Find first set bit = position of first matching byte
rbit  x0, x0                     // reverse bits (MSB <-> LSB)
clz   x0, x0                     // count leading zeros = bit position
// Now x0 / 4 = byte index of first match (for 0x40100401 mask)
```

### Why different masks for different functions

ARM's optimized-routines uses different syndrome masks for different use cases:

```
memchr mask:  0x40100401 (per 32-bit lane)
  - Encodes only char-match positions
  - Each byte gets 2 bits in the syndrome
  - clz result / 4 = byte position

strchr mask:  0xC030_0C03 (per 32-bit lane)
  - Interleaves TWO match results per byte position:
    - Odd bits: character match (found the char we're looking for)
    - Even bits: NUL match (found string terminator)
  - Single syndrome encodes both "found char" and "hit NUL"
  - After rbit+clz: check bit 0 to distinguish char vs NUL match
```

### C/intrinsics implementation

```cpp
#include <arm_neon.h>

// Find first occurrence of 'c' in 16 bytes starting at 'ptr'
// Returns index (0-15) or -1 if not found
int find_byte_neon(const uint8_t* ptr, uint8_t c) {
    // Load 16 bytes and broadcast search byte
    uint8x16_t data = vld1q_u8(ptr);
    uint8x16_t target = vdupq_n_u8(c);

    // Compare: 0xFF for match, 0x00 for no match
    uint8x16_t cmp = vceqq_u8(data, target);

    // Position mask: encodes byte position in 2 bits per byte
    // 0x40100401 repeated = {0x01, 0x04, 0x10, 0x40, 0x01, 0x04, ...}
    static const uint8_t mask_bytes[16] = {
        0x01, 0x04, 0x10, 0x40, 0x01, 0x04, 0x10, 0x40,
        0x01, 0x04, 0x10, 0x40, 0x01, 0x04, 0x10, 0x40
    };
    uint8x16_t mask = vld1q_u8(mask_bytes);

    // Apply mask: only position-encoding bits survive
    uint8x16_t masked = vandq_u8(cmp, mask);

    // Pairwise reduction: 16 bytes -> 8 bytes
    uint8x8_t reduced = vadd_u8(vget_low_u8(masked), vget_high_u8(masked));
    // Further reduce: 8 bytes -> 4 bytes (using pairwise add)
    uint16x4_t r16 = vpaddl_u8(reduced);
    uint32x2_t r32 = vpaddl_u16(r16);
    uint64x1_t r64 = vpaddl_u32(r32);

    // Move to scalar
    uint64_t syndrome = vget_lane_u64(r64, 0);

    if (syndrome == 0) return -1;  // no match

    // Find first set bit
    // __builtin_ctzll gives position of lowest set bit
    int bit_pos = __builtin_ctzll(syndrome);

    // Convert bit position to byte index
    // With 0x40100401 mask, each byte contributes to specific bit positions
    return bit_pos / 2;  // 2 bits per byte position after reduction
}
```

### Simplified alternative using shrn (narrow shift)

A simpler approach used in some implementations: narrow the comparison result to 1 bit per byte using `shrn`, creating a 16-bit bitmask:

```cpp
#include <arm_neon.h>

// Simplified byte finder using shrn (shift right and narrow)
int find_byte_simple(const uint8_t* ptr, uint8_t c) {
    uint8x16_t data = vld1q_u8(ptr);
    uint8x16_t cmp = vceqq_u8(data, vdupq_n_u8(c));

    // Narrow: take bit 7 of each byte, pack into 8-byte result
    // Each 0xFF byte becomes 0x80, each 0x00 stays 0x00
    // After narrowing with shift, we get 1 bit per original byte
    uint16x8_t paired = vreinterpretq_u16_u8(cmp);
    uint8x8_t narrow = vshrn_n_u16(paired, 4);  // take high nibble

    uint64_t bits;
    vst1_u8((uint8_t*)&bits, narrow);

    if (bits == 0) return -1;

    // Count trailing zeros to find first match
    return __builtin_ctzll(bits) / 4;
}
```

### strchr dual-syndrome (char match + NUL detection)

```cpp
// Find char in string, also detecting NUL terminator
// Returns: pointer to char, or NULL if NUL found first
const char* fast_strchr(const char* s, char c) {
    uint8x16_t data = vld1q_u8((const uint8_t*)s);
    uint8x16_t target = vdupq_n_u8((uint8_t)c);

    // Two comparisons: char match and NUL detection
    uint8x16_t char_match = vceqq_u8(data, target);
    uint8x16_t nul_match = vceqq_u8(data, vdupq_n_u8(0));

    // Combine: OR both matches to get "anything interesting happened"
    uint8x16_t any_match = vorrq_u8(char_match, nul_match);

    // Check if any match exists (fast reject)
    uint64_t lo = vgetq_lane_u64(vreinterpretq_u64_u8(any_match), 0);
    uint64_t hi = vgetq_lane_u64(vreinterpretq_u64_u8(any_match), 1);
    if ((lo | hi) == 0) return NULL;  // no match in this chunk

    // Use interleaved syndrome mask to distinguish char vs NUL
    // Odd bit positions: char match, Even bit positions: NUL match
    // ... (apply mask, reduce, rbit+clz, check LSB to distinguish)
}
```

## Expected Impact

| Method | Instructions to find position | Branches |
|--------|------------------------------|----------|
| Lane-by-lane extraction | 16 vgetq_lane + 16 cmp + 16 branches | Up to 16 |
| Syndrome (rbit+clz) | 6 fixed (and, addp, fmov, rbit, clz, lsr) | 0 |
| shrn + ctz | 4 fixed (shrn, fmov, ctz, lsr) | 0 |

The syndrome approach makes byte-finding O(1) instead of O(N). For memchr on large buffers, the main loop checks 32 bytes per iteration with 2 instructions for detection + 6 for position extraction only when a match is found.

## Caveats

- **The magic constants are subtle.** Incorrect mask values produce wrong byte indices. The mask must be chosen so that after pairwise reduction, each original byte position maps to a unique bit position in the 64-bit syndrome. Verify with exhaustive testing.
- **Big-endian vs little-endian.** The `rbit` + `clz` combination assumes little-endian byte ordering. On big-endian systems, use `clz` without `rbit`, or adjust the mask constants.
- **Multiple matches.** The syndrome gives the FIRST match (lowest address). For `strrchr` (find LAST occurrence), reverse the bit operations or use `cls` (count leading sign bits) on the reversed syndrome.
- **Interaction with overlapping-tail.** When the last vector load overlaps with a previous one, the syndrome may report a match in the overlapping region. Adjust the result by the overlap amount.
- **AArch64-only.** The `rbit` instruction is not available on AArch32. On AArch32 NEON, alternative approaches (lookup table, or `vclz` on wider types) are needed.
- **SVE alternative.** On SVE, `brkb` + `incp` replaces the entire syndrome extraction with 2 instructions. Prefer SVE when available.
