---
name: Deferred (Lazy) Computation
source: Cpp-High-Performance Ch.9, game engine optimization patterns
layers: [algorithmic]
platforms: [arm, x86]
keywords: [lazy evaluation, deferred, sqrt, proxy, comparison, squared distance, expensive operation]
---

## Problem

Many programs eagerly compute expensive operations whose results are frequently **discarded without use**. The most common case is computing `sqrt()` for distance values that are only used for comparison -- since `sqrt` is monotonic, comparing squared distances produces the same ordering without the costly operation.

Expensive operations commonly deferred:
- `sqrt()` -- ~15-25 cycles on modern CPUs, vs 1 cycle for a multiply
- `sin()`, `cos()`, `atan2()` -- 20-100 cycles each
- `exp()`, `log()` -- 20-50 cycles
- Division (`/`) -- 10-20 cycles vs 1-4 cycles for multiply
- String formatting, serialization -- 100s-1000s of cycles

The pattern: wrap the deferred value in a proxy object that stores the cheap intermediate form and only computes the expensive final form when explicitly needed.

```cpp
// Wasteful: sqrt is computed for every distance, but most results are only compared
float nearest = INFINITY;
for (auto& point : points) {
    float dx = point.x - target.x;
    float dy = point.y - target.y;
    float dist = std::sqrt(dx*dx + dy*dy);  // 20 cycles * N points
    if (dist < nearest) {
        nearest = dist;
        // ...
    }
}
// sqrt is never needed -- comparison works with squared distances
```

## Detection

**Source-level indicators:**
- `sqrt()` called in a loop where the result is only compared (not used for arithmetic)
- Distance computations followed by threshold comparisons: `if (distance(a, b) < radius)`
- Trigonometric functions computed but only the ratio or sign is used
- `exp()`/`log()` computed for comparison when monotonicity allows comparing the argument directly
- Division where the reciprocal could be precomputed and reused
- Any expensive function whose result often goes unused (early exit, filtering)

**Profile-level indicators:**
- Hot `sqrt` / `__ieee754_sqrt` / `fsqrt` / `vsqrt` in profiler output
- High cycle count in math library functions relative to the algorithm's actual work
- Functions marked as hot that contain expensive operations whose results are conditionally used

## Transformation

### Pattern 1: Squared distance proxy

**Before** -- eager sqrt on every distance:
```cpp
struct Point { float x, y, z; };

float distance(const Point& a, const Point& b) {
    float dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
    return std::sqrt(dx*dx + dy*dy + dz*dz);  // always computed
}

// Usage: comparing distances (sqrt is unnecessary)
if (distance(a, b) < distance(a, c)) { /* ... */ }

// Usage: threshold check (sqrt is unnecessary)
if (distance(player, enemy) < attack_range) { /* ... */ }
```

**After** -- deferred sqrt via proxy:
```cpp
class DistanceProxy {
    float squared_;
public:
    explicit DistanceProxy(float sq) : squared_(sq) {}

    // Comparisons work on squared values -- no sqrt needed
    bool operator<(const DistanceProxy& other) const {
        return squared_ < other.squared_;
    }
    bool operator<(float threshold) const {
        return squared_ < threshold * threshold;  // compare squared
    }
    bool operator>(const DistanceProxy& other) const {
        return squared_ > other.squared_;
    }

    // sqrt only computed when explicitly requesting the float value
    explicit operator float() const {
        return std::sqrt(squared_);
    }

    float squared() const { return squared_; }
};

DistanceProxy distance(const Point& a, const Point& b) {
    float dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
    return DistanceProxy(dx*dx + dy*dy + dz*dz);  // no sqrt
}

// Usage: comparisons are free (no sqrt)
if (distance(a, b) < distance(a, c)) { /* ... */ }

// Usage: threshold -- squares the threshold instead (one multiply vs sqrt)
if (distance(player, enemy) < attack_range) { /* ... */ }

// Usage: when the actual distance is needed
float d = static_cast<float>(distance(a, b));  // sqrt computed here
```

### Pattern 2: Deferred division via reciprocal

```cpp
// Before: repeated division in a loop
for (int i = 0; i < N; i++) {
    result[i] = data[i] / divisor;  // division: 10-20 cycles each
}

// After: precompute reciprocal, multiply instead
float inv_divisor = 1.0f / divisor;  // one division
for (int i = 0; i < N; i++) {
    result[i] = data[i] * inv_divisor;  // multiply: 3-4 cycles each
}
```

### Pattern 3: Lazy string formatting

```cpp
// Before: format string even if log level is too low to print it
void log(Level level, const std::string& msg) {
    if (level >= current_level) output(msg);
}
log(DEBUG, fmt::format("position: ({}, {}, {})", x, y, z));  // always formatted

// After: defer formatting until needed
template<typename... Args>
void log(Level level, fmt::format_string<Args...> fmt, Args&&... args) {
    if (level >= current_level) {
        output(fmt::format(fmt, std::forward<Args>(args)...));  // formatted only if needed
    }
}
log(DEBUG, "position: ({}, {}, {})", x, y, z);  // formatting deferred
```

### Pattern 4: Deferred trigonometric computation

```cpp
// Before: atan2 + sin/cos for angle between vectors
float angle = std::atan2(cross, dot);  // expensive
float sin_a = std::sin(angle);          // expensive
float cos_a = std::cos(angle);          // expensive

// After: use cross product and dot product directly
// cross = |a||b|sin(angle), dot = |a||b|cos(angle)
// If you only need sin and cos, normalize:
float mag = std::sqrt(cross*cross + dot*dot);  // or avoid this too
float sin_a = cross / mag;
float cos_a = dot / mag;
// Eliminated atan2 + sin + cos (60-200 cycles), replaced with sqrt + 2 divides (~35 cycles)
```

## Expected Impact

- **Distance comparisons:** eliminating sqrt saves 15-25 cycles per comparison. In a nearest-neighbor search over N points, this saves `N * 20` cycles per query -- often 30-50% of total loop time.
- **Reciprocal division:** replacing N divisions with 1 division + N multiplications saves `N * 15` cycles approximately. 3-5x speedup for division-heavy loops.
- **Lazy formatting/serialization:** can eliminate 100% of formatting cost for filtered-out log levels. In production where most logs are filtered, this can save millions of cycles per second.
- **No precision loss for comparisons:** squared distance comparisons produce identical results to sqrt-based comparisons for all ordering operations.

## Caveats

- **Proxy objects add API complexity:** the `DistanceProxy` pattern requires all call sites to work with the proxy type. Implicit conversion operators can help but may cause subtle bugs.
- **Overflow risk with squared values:** `float` squared distances overflow at ~1.84e19 (sqrt of FLT_MAX). For world-space coordinates in games/simulations, this is rarely an issue. For scientific computing with large coordinate values, use `double`.
- **Reciprocal precision:** `a / b` and `a * (1/b)` produce different floating-point results due to rounding. For most applications this is negligible, but some numerical algorithms require exact division.
- **Mixed operations with deferred values:** addition and subtraction of distances do not work with squared values (`sqrt(a) + sqrt(b) != sqrt(a + b)`). The proxy must convert to float for any non-comparison operation.
- **Compiler may optimize some cases:** modern compilers with `-ffast-math` can sometimes replace `sqrt` comparisons with squared comparisons automatically. Verify with assembly inspection before adding proxy complexity.
- **Not beneficial when the computed value is always used:** if every `sqrt` result is subsequently used in arithmetic (not just comparison), deferral provides no benefit. Profile first to confirm the operation is frequently discarded.
