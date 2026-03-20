#include <vector>
#include <cmath>

// Scale and clamp — opportunity: NEON vectorization, remove branch
void scale_and_clamp(std::vector<float>& data, float scale, float min_val, float max_val) {
    for (size_t i = 0; i < data.size(); i++) {
        data[i] *= scale;
        if (data[i] < min_val) data[i] = min_val;
        if (data[i] > max_val) data[i] = max_val;
    }
}
