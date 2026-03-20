#include <vector>

// Simple reduction — opportunity: vectorization, multiple accumulators
double sum_array(const std::vector<double>& data) {
    double sum = 0.0;
    for (size_t i = 0; i < data.size(); i++) {
        sum += data[i];
    }
    return sum;
}
