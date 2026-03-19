---
name: Virtual Call Devirtualization (Sort by Type)
source: perf-ninja bad_speculation/virtual_call_mispredict
layers: [source, compiler]
platforms: [arm, x86]
keywords: [virtual call, indirect branch, misprediction, devirtualization, vtable, sort by type, polymorphism]
---

## Problem

Iterating over a polymorphic container and calling virtual methods on randomly-typed objects causes indirect branch target mispredictions. The CPU cannot predict which concrete method will be called because the vtable pointer changes unpredictably from element to element.

With 3 classes (ClassA, ClassB, ClassC) randomly distributed in a vector of 64K objects, the indirect call target is essentially random, causing ~67% misprediction rate.

## Detection

- Profile shows high "bad speculation" or "branch target misprediction" counters
- Hot loop iterating over `std::vector<std::unique_ptr<Base>>` calling virtual methods
- Objects in container are randomly ordered by concrete type
- Indirect call/jump instructions have high misprediction counts in perf annotate

## Transformation

**Before** (from solution.cpp -- random type ordering):
```cpp
// Objects created in random order: ClassA, ClassC, ClassB, ClassA, ClassC, ...
void generateObjects(InstanceArray& array) {
    std::default_random_engine generator(0);
    std::uniform_int_distribution<std::uint32_t> distribution(0, 2);
    for (std::size_t i = 0; i < N; i++) {
        int value = distribution(generator);
        if (value == 0)
            array.push_back(std::make_unique<ClassA>());
        else if (value == 1)
            array.push_back(std::make_unique<ClassB>());
        else
            array.push_back(std::make_unique<ClassC>());
    }
}

void invoke(InstanceArray& array, std::size_t& data) {
    for (const auto& item: array) {
        item->handle(data);  // indirect call -- unpredictable target
    }
}
```

**After** (sort objects by type to make indirect calls predictable):
```cpp
void invoke(InstanceArray& array, std::size_t& data) {
    // Sort by concrete type (vtable pointer) to group same-type objects together
    std::sort(array.begin(), array.end(),
        [](const std::unique_ptr<BaseClass>& a, const std::unique_ptr<BaseClass>& b) {
            return typeid(*a).hash_code() < typeid(*b).hash_code();
        });

    for (const auto& item: array) {
        item->handle(data);  // now predictable: AAAA...BBBB...CCCC...
    }
}
```

**Alternative -- type-specific containers:**
```cpp
std::vector<ClassA> as;
std::vector<ClassB> bs;
std::vector<ClassC> cs;
// Separate by type during construction, iterate each container separately
for (auto& a : as) a.handle(data);
for (auto& b : bs) b.handle(data);
for (auto& c : cs) c.handle(data);
```

**Alternative -- std::variant + std::visit:**
```cpp
using Instance = std::variant<ClassA, ClassB, ClassC>;
std::vector<Instance> array;
// ...
for (auto& item : array) {
    std::visit([&data](auto& obj) { obj.handle(data); }, item);
}
```

## Expected Impact

- 2-5x speedup for loops dominated by indirect call mispredictions
- Sorting by type converts random indirect branches into long predictable runs
- std::variant eliminates indirect calls entirely (replaced by type-switch)

## Caveats

- Sorting has O(N*log(N)) cost -- only worthwhile if the container is iterated many times after sorting
- If the number of concrete types is small (2), modern CPUs can predict the pattern even without sorting
- `std::variant` approach only works when the set of types is known at compile time
- Sorting by type may hurt data locality if objects were originally allocated in cache-friendly order
- If `handle()` is expensive (e.g., hundreds of cycles), the misprediction cost becomes negligible relative to the work
