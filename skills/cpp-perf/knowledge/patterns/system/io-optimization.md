---
name: File I/O Optimization (fstream to mmap/bulk read)
source: perf-ninja misc/io_opt1
layers: [source, system]
platforms: [arm, x86]
keywords: [IO, file read, mmap, fstream, buffered IO, CRC32, memory mapping, bulk read]
---

## Problem

Reading a file one byte at a time via `fstream::read(&c, 1)` incurs enormous overhead per byte: each call goes through the C++ iostream machinery (virtual dispatch, locale facets, sentry objects, buffer management). For a CRC32 computation that processes each byte, this I/O overhead dominates the actual computation.

## Detection

- Profile shows majority of time in iostream/fstream internals, not in computation
- Code calls `fstream::read()`, `fgetc()`, or `istream::get()` in a tight loop
- Per-byte or per-character file reading pattern
- I/O-bound workload where the processing per byte is trivial (CRC, checksum, simple scan)

## Transformation

**Before** (from solution.cpp -- byte-at-a-time fstream read):
```cpp
uint32_t solution(const char *file_name) {
  std::fstream file_stream{file_name};
  if (!file_stream.is_open())
    throw std::runtime_error{"The file could not be opened"};

  uint32_t crc = 0xff'ff'ff'ff;

  char c;
  while (true) {
    file_stream.read(&c, 1);
    if (file_stream.eof())
      break;
    update_crc32(crc, static_cast<uint8_t>(c));
  }

  crc ^= 0xff'ff'ff'ff;
  return crc;
}
```

**After Option 1** (bulk read into buffer):
```cpp
uint32_t solution(const char *file_name) {
  std::ifstream file_stream{file_name, std::ios::binary};
  if (!file_stream.is_open())
    throw std::runtime_error{"The file could not be opened"};

  uint32_t crc = 0xff'ff'ff'ff;

  constexpr size_t BUF_SIZE = 64 * 1024;  // 64KB buffer
  char buffer[BUF_SIZE];

  while (file_stream.read(buffer, BUF_SIZE) || file_stream.gcount() > 0) {
    auto bytes_read = file_stream.gcount();
    for (std::streamsize i = 0; i < bytes_read; ++i) {
      update_crc32(crc, static_cast<uint8_t>(buffer[i]));
    }
  }

  crc ^= 0xff'ff'ff'ff;
  return crc;
}
```

**After Option 2** (mmap -- zero-copy, from MappedFile.hpp pattern):
```cpp
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

uint32_t solution(const char *file_name) {
  int fd = open(file_name, O_RDONLY);
  if (fd == -1)
    throw std::runtime_error{"Could not open file"};

  struct stat sb;
  fstat(fd, &sb);
  size_t file_size = sb.st_size;

  void *mapped = mmap(nullptr, file_size, PROT_READ, MAP_PRIVATE, fd, 0);
  close(fd);

  if (mapped == MAP_FAILED)
    throw std::runtime_error{"Could not mmap file"};

  const uint8_t *data = static_cast<const uint8_t*>(mapped);
  uint32_t crc = 0xff'ff'ff'ff;

  for (size_t i = 0; i < file_size; ++i) {
    update_crc32(crc, data[i]);
  }

  crc ^= 0xff'ff'ff'ff;
  munmap(mapped, file_size);
  return crc;
}
```

**Windows equivalent** uses `CreateFileMapping` / `MapViewOfFile`.

## Expected Impact

- 10-100x speedup over byte-at-a-time fstream reads
- Bulk read (64KB buffer): ~10-20x speedup, portable
- mmap: ~20-100x speedup, eliminates copy from kernel to user buffer
- For a 10MB file: byte-at-a-time ~500ms, bulk read ~5ms, mmap ~2ms (typical)

## Caveats

- mmap is not portable to all platforms without abstraction (Linux/macOS vs Windows API differs)
- mmap may not perform well on network filesystems or extremely large files (address space pressure on 32-bit)
- For small files (<4KB), the mmap setup overhead may exceed the benefit over bulk read
- Bulk read with a large buffer is simpler, portable, and nearly as fast for sequential access
- mmap is read-only by default; modifying mapped memory requires MAP_SHARED and careful synchronization
- Neither approach helps if the bottleneck is actual disk I/O (spinning disk, network) rather than API overhead
- C `fread()` with large buffers is also very fast and simpler than mmap
