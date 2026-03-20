#include <array>

constexpr int N = 128;
using Matrix = std::array<std::array<float, N>, N>;

void multiply(Matrix& result, const Matrix& a, const Matrix& b) {
    for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++) {
            result[i][j] = 0;
            for (int k = 0; k < N; k++)
                result[i][j] += a[i][k] * b[k][j];
        }
}
