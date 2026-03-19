---
name: SIMD String/Character Search (Longest Line)
source: perf-ninja core_bound/compiler_intrinsics_2
layers: [intrinsic, source]
platforms: [arm, x86]
keywords: [SIMD, intrinsics, string search, newline, character scan, SSE2, NEON, CMOV, longest line]
---

## Problem

Scanning a string character-by-character to find specific characters (e.g., newlines) is limited to 1 byte/cycle throughput. SIMD instructions can compare 16-64 bytes in parallel, dramatically accelerating character search patterns.

The baseline uses a scalar loop with ternary/CMOV to find the longest line:

```cpp
for (auto s : inputContents) {
  curLineLength = (s == '\n') ? 0 : curLineLength + 1;
  longestLine = std::max(curLineLength, longestLine);
}
```

This has a loop-carried dependency chain through `curLineLength` that limits ILP.

## Detection

- Byte-by-byte character scanning in a loop (`== '\n'`, `== ','`, etc.)
- String processing with character-at-a-time comparison
- Profile shows the loop is memory/core bound with low IPC
- Functions like `strchr`, `memchr` reimplemented as scalar loops

## Transformation

**Before** (scalar, from solution.cpp):
```cpp
unsigned solution(const std::string &inputContents) {
  unsigned longestLine = 0;
  unsigned curLineLength = 0;

  for (auto s : inputContents) {
    curLineLength = (s == '\n') ? 0 : curLineLength + 1;
    longestLine = std::max(curLineLength, longestLine);
  }

  return longestLine;
}
```

**After** (SIMD approach -- x86 SSE2 example):
```cpp
#include <immintrin.h>

unsigned solution(const std::string &inputContents) {
  unsigned longestLine = 0;
  unsigned curLineLength = 0;
  const char *data = inputContents.data();
  size_t len = inputContents.size();
  size_t i = 0;

  __m128i newline = _mm_set1_epi8('\n');

  for (; i + 16 <= len; ) {
    __m128i chunk = _mm_loadu_si128((__m128i*)(data + i));
    __m128i cmp = _mm_cmpeq_epi8(chunk, newline);
    int mask = _mm_movemask_epi8(cmp);

    if (mask == 0) {
      // No newlines in this 16-byte block
      curLineLength += 16;
      i += 16;
    } else {
      // Process newline positions via bitmask
      while (mask) {
        int pos = __builtin_ctz(mask);
        curLineLength += pos;
        longestLine = std::max(longestLine, curLineLength);
        curLineLength = 0;
        mask >>= (pos + 1);
        i += pos + 1;
        // Reload for next segment within block
      }
    }
  }

  // Scalar tail
  for (; i < len; i++) {
    curLineLength = (data[i] == '\n') ? 0 : curLineLength + 1;
    longestLine = std::max(longestLine, curLineLength);
  }
  longestLine = std::max(longestLine, curLineLength);

  return longestLine;
}
```

**ARM NEON equivalent pattern:**
```cpp
uint8x16_t newline = vdupq_n_u8('\n');
uint8x16_t chunk = vld1q_u8((const uint8_t*)(data + i));
uint8x16_t cmp = vceqq_u8(chunk, newline);
// Use vmaxvq_u8 or horizontal reduction to check for matches
```

## Expected Impact

- 4-16x speedup depending on line length distribution and SIMD width
- Processes 16 bytes (SSE2) or 32 bytes (AVX2) or 64 bytes (AVX-512) per iteration
- Most beneficial when newlines are sparse (long lines)

## Caveats

- Speedup is data-dependent: if lines are very short (many newlines), scalar CMOV can be competitive
- Requires careful tail handling for non-aligned/non-multiple-of-16 sizes
- Platform-specific intrinsics reduce portability; consider `std::find` or `memchr` first as they often use SIMD internally
- On ARM, extracting individual match positions from NEON results requires different bit manipulation than x86
