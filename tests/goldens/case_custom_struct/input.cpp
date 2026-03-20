#include <vector>
#include <cmath>

struct Point3D {
    float x, y, z;
};

// Compute distances between all pairs — opportunity: deferred sqrt, SIMD
float total_distance(const std::vector<Point3D>& points) {
    float total = 0.0f;
    for (size_t i = 0; i < points.size(); i++) {
        for (size_t j = i + 1; j < points.size(); j++) {
            float dx = points[i].x - points[j].x;
            float dy = points[i].y - points[j].y;
            float dz = points[i].z - points[j].z;
            total += std::sqrt(dx*dx + dy*dy + dz*dz);
        }
    }
    return total;
}
