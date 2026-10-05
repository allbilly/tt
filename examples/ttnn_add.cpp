#include <algorithm>
#include <iostream>
#include <stdexcept>

#include <ttnn/device.hpp>
#include <ttnn/operations/creation/creation.hpp>
#include <ttnn/operations/eltwise/binary/binary.hpp>

int main() {
    auto device = ttnn::open_mesh_device(0);
    {
        auto a = ttnn::ones(ttnn::Shape({32, 32}),
                            ttnn::DataType::BFLOAT16, ttnn::Layout::TILE,
                            std::ref(*device));
        auto b = ttnn::add(a, a);
        auto c = ttnn::add(a, b);
        auto values = c.to_vector<bfloat16>();
        if (values.size() != 1024 ||
            !std::all_of(values.begin(), values.end(), [](bfloat16 value) {
                return static_cast<float>(value) == 3.0f;
            })) {
            throw std::runtime_error("TTNN ADD output mismatch");
        }
        std::cout << "PASS: TTNN device ADD, 1024 BF16 values equal 3.0\n";
    }
    ttnn::close_device(*device);
}
